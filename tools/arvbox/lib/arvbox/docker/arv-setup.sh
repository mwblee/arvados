#!/bin/bash
# Copyright (C) The Arvados Authors. All rights reserved.
#
# SPDX-License-Identifier: AGPL-3.0

exec 2>&1
set -eux -o pipefail

. ~/arvados/tools/arvbox/lib/arvbox/docker/common.sh

cd ~/arvados/sdk/cli
run_bundler --binstubs=$PWD/binstubs
ln -sf ~/arvados/sdk/cli/binstubs/arv /usr/local/bin/arv
