#!/bin/bash
set -e
D=/home/deck/homebrew/plugins/decky-romm-sync
cp ${D}/dist/index.js ${D}/dist/index.js.bak
cp ${D}/main.py ${D}/main.py.bak
cp ${D}/py_modules/services/library.py ${D}/py_modules/services/library.py.bak
cp ${D}/py_modules/services/artwork.py ${D}/py_modules/services/artwork.py.bak
cp ${D}/py_modules/domain/shortcut_data.py ${D}/py_modules/domain/shortcut_data.py.bak
cp /tmp/decky-index.js ${D}/dist/index.js
cp /tmp/decky-main.py ${D}/main.py
cp /tmp/decky-library.py ${D}/py_modules/services/library.py
cp /tmp/decky-artwork.py ${D}/py_modules/services/artwork.py
cp /tmp/decky-shortcut_data.py ${D}/py_modules/domain/shortcut_data.py
chown root:root ${D}/dist/index.js ${D}/main.py ${D}/py_modules/services/library.py ${D}/py_modules/services/artwork.py ${D}/py_modules/domain/shortcut_data.py
echo DEPLOYED_OK
ls -la ${D}/dist/index.js ${D}/main.py ${D}/py_modules/services/library.py ${D}/py_modules/services/artwork.py ${D}/py_modules/domain/shortcut_data.py
