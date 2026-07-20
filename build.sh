#!/usr/bin/env bash

set -euo pipefail

# 清理旧的构建产物，防止重复上传引发 PyPI 400 报错
rm -rf dist yyds_pip.egg-info

python -m pytest
python -m build
python -m twine check dist/*

if [[ "${1:-}" == "--upload" ]]; then
    python -m twine upload dist/*
else
    echo "Build verified. Pass --upload to publish to PyPI."
fi
