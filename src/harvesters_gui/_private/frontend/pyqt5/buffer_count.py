#!/usr/bin/env python3
# ----------------------------------------------------------------------------
#
# Copyright 2026 harvesters_monitor contributors
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


# Standard library imports

# Related third party imports
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QWidget

# Local application/library specific imports
from harvesters_gui._private.frontend.pyqt5.helper import get_system_font


class BufferCountSetting(QWidget):
    #
    # A toolbar widget that exposes the number of acquisition buffers
    # that the ImageAcquirer announces. The value takes effect when the
    # acquisition starts, because that is when the buffers are
    # allocated and announced to the GenTL producer.
    #

    value_changed = pyqtSignal(int)

    _DEFAULT_VALUE = 10
    _MAXIMUM_VALUE = 256

    def __init__(self, parent=None):
        #
        super().__init__(parent)

        # Match the font of the other toolbar widgets (they all set
        # the system font explicitly):
        self.setFont(get_system_font())

        #
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)

        #
        self._label = QLabel('Buffer', self)
        self._label.setFont(get_system_font())
        self._label.setAlignment(Qt.AlignVCenter)

        self._spin_box = QSpinBox(self)
        self._spin_box.setFont(get_system_font())
        self._spin_box.setRange(1, self._MAXIMUM_VALUE)
        self._spin_box.setValue(self._DEFAULT_VALUE)
        self._spin_box.setMinimumWidth(64)
        self._spin_box.setFixedHeight(self._spin_box.sizeHint().height())

        #
        layout.addWidget(self._label)
        layout.addWidget(self._spin_box)

        #
        self._spin_box.valueChanged.connect(self.value_changed)

    def value(self):
        return self._spin_box.value()

    def set_value(self, value):
        self._spin_box.setValue(value)

    def set_minimum(self, value):
        #
        # The GenTL producer requires at least this many buffers; the
        # spin box refuses anything below it.
        #
        self._spin_box.setMinimum(max(1, int(value)))
