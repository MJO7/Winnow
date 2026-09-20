#!/usr/bin/env bash
# Package the four Lambda zips into build/. Vendors src/winnow and
# lambdas/*.py plus arm64 manylinux wheels for python3.12. boto3 is
# provided by the runtime and excluded. No torch: the API retrieves with
# tsvector (docs/adr/0004), which keeps each zip well under 50 MB.
set -euo pipefail
cd "$(dirname "$0")/.."
ARCH="${LAMBDA_ARCH:-aarch64}"
PY="3.12"
STAGE=build/stage
rm -rf "$STAGE" && mkdir -p "$STAGE" build

# uv resolves wheels for a foreign platform without needing that interpreter locally.
uv pip install -q \
  --python-platform "${ARCH}-manylinux2014" --python-version "$PY" --only-binary :all: \
  --target "$STAGE" \
  "psycopg[binary]>=3.1" "requests>=2.31" "python-dotenv>=1.0" "pgvector>=0.3" "anthropic>=0.40" "pydantic>=2"

cp -R src/winnow "$STAGE/winnow"
cp lambdas/*.py "$STAGE/"
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$STAGE" -name "*.dist-info" -type d -prune -exec rm -rf {} +

for fn in discover fetch label api; do
  rm -f "build/lambda_${fn}.zip"
  (cd "$STAGE" && zip -qr "../lambda_${fn}.zip" .)
  echo "build/lambda_${fn}.zip  $(du -h "build/lambda_${fn}.zip" | cut -f1)"
done
