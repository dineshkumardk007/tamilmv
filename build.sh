#!/usr/bin/env bash
# Exit on error
set -o errexit

echo "===> Installing Python dependencies..."
pip install -r requirements.txt

echo "===> Installing Node.js streamer dependencies..."
npm install --omit=dev

echo "===> Build finished successfully."
