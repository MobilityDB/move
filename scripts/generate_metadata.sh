#!/bin/bash
# scripts/generate_metadata.sh

# Get version from command line argument or use default
VERSION=${1:-"0.0.0-dev"}

# Path to template and output files (accounting for script being in scripts/ directory)
TEMPLATE_PATH="$(dirname "$0")/../metadata.in.txt"
OUTPUT_PATH="$(dirname "$0")/../metadata.txt"

# Check if template exists
if [ ! -f "$TEMPLATE_PATH" ]; then
  echo "Error: Template file not found at $TEMPLATE_PATH"
  exit 1
fi

# Generate metadata.txt with version
sed "s/@MOVE_VERSION@/$VERSION/" "$TEMPLATE_PATH" > "$OUTPUT_PATH"
echo "Generated metadata.txt with version $VERSION"
