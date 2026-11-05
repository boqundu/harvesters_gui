#!/usr/bin/env python3
# ----------------------------------------------------------------------------
#
# Copyright 2021 EMVA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ----------------------------------------------------------------------------


import sys
import traceback
from PyQt5.QtWidgets import QApplication, QMessageBox
from harvesters_gui.frontend.pyqt5 import Harvester


def _show_exception(exc_type, exc_value, exc_tb):
    # Without an application-installed excepthook, PyQt5 aborts the
    # process when an exception escapes a Qt slot; log it and turn it
    # into a dialog instead so that the GUI stays alive:
    traceback.print_exception(exc_type, exc_value, exc_tb)
    try:
        QMessageBox.warning(
            None, 'Unexpected error',
            'An unexpected error occurred:\n\n{}'.format(exc_value))
    except Exception:
        pass


if __name__ == "__main__":
    sys.excepthook = _show_exception
    app = QApplication(sys.argv)
    h = Harvester()
    h.show()
    sys.exit(app.exec_())