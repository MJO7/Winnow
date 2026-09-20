from winnow.normalize import extract_signature


def ts(line: str) -> str:
    return f"2026-09-19T10:15:22.1234567Z {line}"


def test_pytest_short_summary_is_preferred_source():
    log = "\n".join(
        [
            ts("=========================== FAILURES ============================"),
            ts("______________________ TestFoo.test_bar _________________________"),
            ts("    def test_bar(self):"),
            ts(">       assert 1 == 2"),
            ts("E       assert 1 == 2"),
            ts("tests/test_foo.py:42: AssertionError"),
            ts("=========================== short test summary info =============="),
            ts("FAILED tests/test_foo.py::TestFoo::test_bar - AssertionError: assert 1 == 2"),
            ts("=========================== 1 failed in 3.21s ===================="),
        ]
    )
    sig = extract_signature(log)
    assert sig.signature_source == "pytest_failed_summary"
    assert sig.exception_type == "AssertionError"
    assert sig.test_nodeids == ["tests/test_foo.py::TestFoo::test_bar"]
    assert "3.21s" not in sig.message_skeleton  # duration in the summary line, not this one, but sanity


def test_multiple_pytest_failures_collects_all_nodeids():
    log = "\n".join(
        [
            ts("FAILED tests/a.py::test_one - AssertionError: assert 1 == 2"),
            ts("ERROR tests/b.py::test_two - ConnectionError: [Errno 111] Connection refused"),
        ]
    )
    sig = extract_signature(log)
    assert sig.signature_source == "pytest_failed_summary"
    assert sig.test_nodeids == ["tests/a.py::test_one", "tests/b.py::test_two"]
    # signature keys off the FIRST failure, deterministically
    assert sig.exception_type == "AssertionError"


def test_bare_traceback_extracts_exception_and_frames():
    log = "\n".join(
        [
            ts("Traceback (most recent call last):"),
            ts('  File "/home/runner/work/repo/repo/script.py", line 10, in <module>'),
            ts("    main()"),
            ts('  File "/home/runner/work/repo/repo/script.py", line 7, in main'),
            ts('    raise ValueError("bad thing at 0x7f8a1c0b3d90 in /tmp/tmpabcdef12/data")'),
            ts("ValueError: bad thing at 0x7f8a1c0b3d90 in /tmp/tmpabcdef12/data"),
        ]
    )
    sig = extract_signature(log)
    assert sig.signature_source == "traceback_block"
    assert sig.exception_type == "ValueError"
    assert "<ADDR>" in sig.message_skeleton
    assert "<TMP>" in sig.message_skeleton
    assert "0x7f8a1c0b3d90" not in sig.message_skeleton
    # closest-to-error frame (main) should be first
    assert sig.top_frames[0].function == "main"


def test_last_traceback_block_wins_when_multiple_present():
    log = "\n".join(
        [
            ts("Traceback (most recent call last):"),
            ts('  File "a.py", line 1, in f1'),
            ts("KeyError: 'irrelevant, caught and logged'"),
            ts("some other output in between"),
            ts("Traceback (most recent call last):"),
            ts('  File "b.py", line 2, in f2'),
            ts("RuntimeError: the real terminal failure"),
        ]
    )
    sig = extract_signature(log)
    assert sig.exception_type == "RuntimeError"
    assert sig.top_frames[0].function == "f2"


def test_no_traceback_or_summary_falls_back_to_tail():
    log = "\n".join(
        [
            ts("Starting job..."),
            ts("Running step 1"),
            ts("Error: Process completed with exit code 137."),
        ]
    )
    sig = extract_signature(log)
    assert sig.signature_source == "tail_fallback"
    assert "exit code" in sig.message_skeleton


def test_signature_hash_is_stable_across_equivalent_but_differently_timed_logs():
    log_a = "\n".join(
        [
            "2026-09-19T10:15:22.0000000Z FAILED tests/a.py::test_one - AssertionError: assert 1 == 2",
        ]
    )
    log_b = "\n".join(
        [
            "2026-09-20T03:41:09.9999999Z FAILED tests/a.py::test_one - AssertionError: assert 1 == 2",
        ]
    )
    assert extract_signature(log_a).signature_hash == extract_signature(log_b).signature_hash


def test_signature_hash_differs_for_different_exceptions():
    sig1 = extract_signature(ts("FAILED tests/a.py::test_one - AssertionError: assert 1 == 2"))
    sig2 = extract_signature(ts("FAILED tests/a.py::test_one - ValueError: bad value"))
    assert sig1.signature_hash != sig2.signature_hash


def test_windows_temp_path_normalized():
    log = ts(
        r'RuntimeError: could not remove C:\Users\runneradmin\AppData\Local\Temp\tmp3f8a\file.txt'
    )
    sig = extract_signature(log)
    assert "<TMP>" in sig.message_skeleton
    assert "runneradmin" not in sig.message_skeleton


def test_run_ids_and_large_numbers_generalized():
    sig = extract_signature(ts("FAILED t.py::x - TimeoutError: run 123456789 did not complete"))
    assert "<NUM>" in sig.message_skeleton
    assert "123456789" not in sig.message_skeleton
