#!/bin/bash
# Install ODA File Converter for DWG-to-DXF conversion.
# Required for extracting structured data from DWG files (richer than PDF fallback).
# Downloads the QT6 Linux x64 .deb package from Open Design Alliance.

set -e

ODA_URL="https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_27.1.deb"
DEB_PATH="/tmp/ODAFileConverter.deb"

# Check if already installed
if command -v ODAFileConverter &>/dev/null; then
    echo "ODA File Converter is already installed at: $(which ODAFileConverter)"
    ODAFileConverter 2>&1 | head -1 || true
    exit 0
fi

echo "Downloading ODA File Converter..."
wget -q --show-progress -O "$DEB_PATH" "$ODA_URL"

echo "Installing ODA File Converter (requires sudo)..."
sudo dpkg -i "$DEB_PATH"

# Verify
if command -v ODAFileConverter &>/dev/null; then
    echo "ODA File Converter installed successfully at: $(which ODAFileConverter)"
else
    echo "ERROR: Installation failed. ODAFileConverter not found in PATH."
    exit 1
fi

rm -f "$DEB_PATH"
echo "Done. DWG files will now be auto-converted to DXF during extraction."
