#!/bin/bash
# Copyright (C) The Arvados Authors. All rights reserved.
#
# SPDX-License-Identifier: AGPL-3.0

exec 2>&1
sleep 2
set -eux -o pipefail

. /usr/local/lib/arvbox/common.sh
. /usr/local/lib/arvbox/go-setup.sh

flock /var/lib/gopath/gopath.lock go get -t "git.curoverse.com/arvados.git/services/keep-balance"
install $GOPATH/bin/keep-balance /usr/local/bin

if test "$1" = "--only-deps" ; then
    exit
fi

export ARVADOS_API_HOST=$localip:${services[api]}
export ARVADOS_API_HOST_INSECURE=1
export ARVADOS_API_TOKEN=$(cat /var/lib/arvados/superuser_token)
