#!/bin/bash

echo "Running containers:"
docker ps -a
echo "Fixing permissions..."
chmod -R 777 ./
echo "Copying config files..."
cp ./dev/tests/settings_local.py ./ietf/settings_local.py
echo "Ensure all requirements.txt packages are installed..."
pip --disable-pip-version-check --no-cache-dir install -r requirements.txt
echo "Compiling native node packages..."
yarn rebuild
echo "Building static assets..."
yarn build
yarn legacy:build
echo "Creating data directories..."
chmod +x ./docker/scripts/app-create-dirs.sh
./docker/scripts/app-create-dirs.sh
echo "Fetching latest coverage results file..."
curl -fsSL https://github.com/ietf-tools/datatracker/releases/download/baseline/coverage.json -o release-coverage.json
psql -U django -h db -d ietf -v ON_ERROR_STOP=1 -c '\x' -c 'ALTER USER django set search_path=datatracker,public;'
