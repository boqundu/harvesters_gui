#!/usr/bin/env python3
# ----------------------------------------------------------------------------
#
# Copyright 2018 EMVA
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
import datetime
import os
import sys
import time

# Related third party imports
import psutil
from PyQt5.QtCore import QMutexLocker, QMutex, pyqtSignal, QThread
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QMainWindow, QAction, QComboBox, \
    QDesktopWidget, QFileDialog, QDialog, QShortcut, QApplication, \
    QLabel, QMessageBox

# Local application/library specific imports
from harvesters.core import Harvester as HarvesterCore, ParameterSet, ParameterKey
from harvesters.util.buffer_policy import compute_num_buffers
from harvesters_gui._private.frontend.canvas import Canvas2D
from harvesters_gui._private.frontend.helper import compose_tooltip
from harvesters_gui._private.frontend.pyqt5.about import About
from harvesters_gui._private.frontend.pyqt5.action import Action
from harvesters_gui._private.frontend.pyqt5.attribute_controller import AttributeController
from harvesters_gui._private.frontend.pyqt5.device_list import ComboBoxDeviceList
from harvesters_gui._private.frontend.pyqt5.buffer_count import BufferCountSetting
from harvesters_gui._private.frontend.pyqt5.display_rate_list import ComboBoxDisplayRateList
from harvesters_gui._private.frontend.pyqt5.helper import get_system_font
from harvesters_gui._private.frontend.pyqt5.icon import Icon
from harvesters_gui._private.frontend.pyqt5.thread import _PyQtThread
from harvesters.util.logging import get_logger


def read_node_value(node_map, name):
    #
    # Reads the value of one of the remote device's nodes; a node that
    # the device does not implement is reported as '?' instead of
    # raising, so that the callers can keep their display and sizing
    # logic simple.
    #
    try:
        return getattr(node_map, name).value
    except (AttributeError, RuntimeError, ValueError):
        return '?'


class Harvester(QMainWindow):
    #
    _signal_update_statistics = pyqtSignal(str)
    _signal_stop_image_acquisition = pyqtSignal()

    def __init__(self, *, vsync=True, logger=None):
        #
        self._logger = logger or get_logger(name='harvesters')

        #
        super().__init__()

        #
        self._mutex = QMutex()

        self._debug_stats = 'HARVESTER_STATS_DEBUG' in os.environ
        self._debug_previous_images = None

        profile = True if 'HARVESTER_PROFILE' in os.environ else False
        self._harvester_core = HarvesterCore(
            profile=profile, logger=self._logger
        )
        self._ia = None  # Image Acquirer

        #
        # Vertical sync can be turned off with HARVESTER_VSYNC=0 for
        # diagnostics; a blocked swap shows up directly in the frame
        # rate of the display:
        #
        if os.environ.get('HARVESTER_VSYNC') == '0':
            vsync = False
        self._widget_canvas = Canvas2D(vsync=vsync)
        self._widget_canvas.create_native()
        self._widget_canvas.native.setParent(self)

        #
        self._action_stop_image_acquisition = None

        #
        self._observer_widgets = []

        #
        self._widget_device_list = None
        self._widget_status_bar = None
        self._widget_main = None
        self._widget_about = None
        self._widget_attribute_controller = None

        #
        self._signal_update_statistics.connect(self.update_statistics)
        self._signal_stop_image_acquisition.connect(self._stop_image_acquisition)
        self._thread_statistics_measurement = _PyQtThread(
            parent=self, mutex=self._mutex,
            worker=self._worker_update_statistics,
            update_cycle_us=250000
        )

        #
        self._initialize_widgets()

        #
        for o in self._observer_widgets:
            o.update()

    def _stop_image_acquisition(self):
        self.action_stop_image_acquisition.execute()

    def update_statistics(self, message):
        self._status_label.setText(message)

    def closeEvent(self, event):
        # Stop the statistics worker before destroying the ImageAcquirer.
        if self._thread_statistics_measurement.is_running():
            self._thread_statistics_measurement.stop()

        if self.ia is not None:
            self.canvas.release_buffers()
            try:
                if self.ia.is_acquiring():
                    self.ia.stop()
            except Exception:
                self._logger.exception('Failed to stop image acquisition on close.')

        if self._widget_attribute_controller:
            self._widget_attribute_controller.close()
            self._widget_attribute_controller = None

        if self._harvester_core:
            self._harvester_core.reset()

        self._ia = None
        self.canvas.ia = None
        super().closeEvent(event)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._harvester_core.reset()

    @property
    def canvas(self):
        return self._widget_canvas

    @property
    def attribute_controller(self):
        return self._widget_attribute_controller

    @property
    def about(self):
        return self._widget_about

    @property
    def version(self):
        return self.harvester_core.version

    @property
    def device_list(self):
        return self._widget_device_list

    @property
    def cti_files(self):
        return self.harvester_core.files

    @property
    def harvester_core(self):
        return self._harvester_core

    def _initialize_widgets(self):
        #
        self.setWindowIcon(Icon('genicam_logo_i.png'))

        #
        self.setWindowTitle('GenICam.Harvester')
        self.setFont(get_system_font())

        #
        self.statusBar().showMessage('')
        self.statusBar().setFont(get_system_font())

        #
        # A permanent label for the status bar message:
        self._status_label = QLabel()
        self.statusBar().addPermanentWidget(self._status_label)

        #
        self._initialize_gui_toolbar(self._observer_widgets)

        #
        self.setCentralWidget(self.canvas.native)

        #
        self.resize(800, 600)

        # Place it in the center.
        rectangle = self.frameGeometry()
        coordinate = QDesktopWidget().availableGeometry().center()
        rectangle.moveCenter(coordinate)
        self.move(rectangle.topLeft())

    def _initialize_gui_toolbar(self, observers):
        #
        group_gentl_info = self.addToolBar('GenTL Information')
        group_connection = self.addToolBar('Connection')
        group_device = self.addToolBar('Image Acquisition')
        group_display = self.addToolBar('Display')
        group_help = self.addToolBar('Help')

        # Create buttons:

        #
        button_select_file = ActionSelectFile(
            icon='open_file.png', title='Select file', parent=self,
            action=self.action_on_select_file,
            is_enabled=self.is_enabled_on_select_file
        )
        shortcut_key = 'Ctrl+o'
        button_select_file.setToolTip(
            compose_tooltip('Open a CTI file to load', shortcut_key)
        )
        button_select_file.setShortcut(shortcut_key)
        button_select_file.toggle()
        observers.append(button_select_file)

        #
        button_update = ActionUpdateList(
            icon='update.png', title='Update device list', parent=self,
            action=self.action_on_update_list,
            is_enabled=self.is_enabled_on_update_list
        )
        shortcut_key = 'Ctrl+u'
        button_update.setToolTip(
            compose_tooltip('Update the device list', shortcut_key)
        )
        button_update.setShortcut(shortcut_key)
        button_update.toggle()
        observers.append(button_update)

        #
        button_connect = ActionConnect(
            icon='connect.png', title='Connect', parent=self,
            action=self.action_on_connect,
            is_enabled=self.is_enabled_on_connect
        )
        shortcut_key = 'Ctrl+c'
        button_connect.setToolTip(
            compose_tooltip(
                'Connect the selected device to Harvester',
                shortcut_key
            )
        )
        button_connect.setShortcut(shortcut_key)
        button_connect.toggle()
        observers.append(button_connect)

        #
        button_disconnect = ActionDisconnect(
            icon='disconnect.png', title='Disconnect', parent=self,
            action=self.action_on_disconnect,
            is_enabled=self.is_enabled_on_disconnect
        )
        shortcut_key = 'Ctrl+d'
        button_disconnect.setToolTip(
            compose_tooltip(
                'Disconnect the device from Harvester',
                shortcut_key
            )
        )
        button_disconnect.setShortcut(shortcut_key)
        button_disconnect.toggle()
        observers.append(button_disconnect)

        #
        button_start_image_acquisition = ActionStartImageAcquisition(
            icon='start_acquisition.png', title='Start Acquisition', parent=self,
            action=self.action_on_start_image_acquisition,
            is_enabled=self.is_enabled_on_start_image_acquisition
        )
        shortcut_key = 'Ctrl+j'
        button_start_image_acquisition.setToolTip(
            compose_tooltip('Start image acquisition', shortcut_key)
        )
        button_start_image_acquisition.setShortcut(shortcut_key)
        button_start_image_acquisition.toggle()
        observers.append(button_start_image_acquisition)

        #
        button_toggle_drawing = ActionToggleDrawing(
            icon='pause.png', title='Pause/Resume Drawing', parent=self,
            action=self.action_on_toggle_drawing,
            is_enabled=self.is_enabled_on_toggle_drawing
        )
        shortcut_key = 'Ctrl+k'
        button_toggle_drawing.setToolTip(
            compose_tooltip('Pause/Resume drawing', shortcut_key)
        )
        button_toggle_drawing.setShortcut(shortcut_key)
        button_toggle_drawing.toggle()
        observers.append(button_toggle_drawing)

        #
        button_stop_image_acquisition = ActionStopImageAcquisition(
            icon='stop_acquisition.png', title='Stop Acquisition', parent=self,
            action=self.action_on_stop_image_acquisition,
            is_enabled=self.is_enabled_on_stop_image_acquisition
        )
        shortcut_key = 'Ctrl+l'
        button_stop_image_acquisition.setToolTip(
            compose_tooltip('Stop image acquisition', shortcut_key)
        )
        button_stop_image_acquisition.setShortcut(shortcut_key)
        button_stop_image_acquisition.toggle()
        observers.append(button_stop_image_acquisition)
        self._action_stop_image_acquisition = button_stop_image_acquisition

        #
        button_dev_attribute = ActionShowAttributeController(
            icon='device_attribute.png', title='Device Attribute', parent=self,
            action=self.action_on_show_attribute_controller,
            is_enabled=self.is_enabled_on_show_attribute_controller
        )
        shortcut_key = 'Ctrl+a'
        button_dev_attribute.setToolTip(
            compose_tooltip('Edit device attribute', shortcut_key)
        )
        button_dev_attribute.setShortcut(shortcut_key)
        button_dev_attribute.toggle()
        observers.append(button_dev_attribute)

        # Create widgets to add:

        #
        self._widget_device_list = ComboBoxDeviceList(self)
        self._widget_device_list.setSizeAdjustPolicy(
            QComboBox.AdjustToContents
        )
        shortcut_key = 'Ctrl+Shift+d'
        shortcut = QShortcut(QKeySequence(shortcut_key), self)

        def show_popup():
            self._widget_device_list.showPopup()

        shortcut.activated.connect(show_popup)
        self._widget_device_list.setToolTip(
            compose_tooltip('Select a device to connect', shortcut_key)
        )
        observers.append(self._widget_device_list)
        for d in self.harvester_core.device_info_list:
            self._widget_device_list.addItem(d)
        group_connection.addWidget(self._widget_device_list)
        observers.append(self._widget_device_list)

        #
        self._widget_display_rates = ComboBoxDisplayRateList(self)
        self._widget_display_rates.setSizeAdjustPolicy(
            QComboBox.AdjustToContents
        )
        shortcut_key = 'Ctrl+Shift+r'
        shortcut = QShortcut(QKeySequence(shortcut_key), self)

        def show_popup():
            self._widget_display_rates.showPopup()

        shortcut.activated.connect(show_popup)
        self._widget_display_rates.setToolTip(
            compose_tooltip('Select a display rate', shortcut_key)
        )
        observers.append(self._widget_display_rates)
        self._widget_display_rates.setEnabled(True)
        group_display.addWidget(self._widget_display_rates)
        observers.append(self._widget_display_rates)

        #
        self._widget_buffer_count = BufferCountSetting(self)
        self._widget_buffer_count.setToolTip(
            'Number of acquisition buffers; the value takes effect at '
            'the next acquisition start'
        )
        self._widget_buffer_count.setEnabled(False)
        self._widget_buffer_count.value_changed.connect(
            self._on_num_buffers_changed
        )
        group_display.addWidget(self._widget_buffer_count)

        #
        self._widget_about = About(self)
        button_about = ActionShowAbout(
            icon='about.png', title='About', parent=self,
            action=self.action_on_show_about
        )
        button_about.setToolTip(
            compose_tooltip('Show information about Harvester')
        )
        button_about.toggle()
        observers.append(button_about)

        # Configure observers:

        #
        button_select_file.add_observer(button_update)
        button_select_file.add_observer(button_connect)
        button_select_file.add_observer(button_disconnect)
        button_select_file.add_observer(button_dev_attribute)
        button_select_file.add_observer(button_start_image_acquisition)
        button_select_file.add_observer(button_toggle_drawing)
        button_select_file.add_observer(button_stop_image_acquisition)
        button_select_file.add_observer(self._widget_device_list)

        #
        button_update.add_observer(self._widget_device_list)
        button_update.add_observer(button_connect)

        #
        button_connect.add_observer(button_select_file)
        button_connect.add_observer(button_update)
        button_connect.add_observer(button_disconnect)
        button_connect.add_observer(button_dev_attribute)
        button_connect.add_observer(button_start_image_acquisition)
        button_connect.add_observer(button_toggle_drawing)
        button_connect.add_observer(button_stop_image_acquisition)
        button_connect.add_observer(self._widget_device_list)

        #
        button_disconnect.add_observer(button_select_file)
        button_disconnect.add_observer(button_update)
        button_disconnect.add_observer(button_connect)
        button_disconnect.add_observer(button_dev_attribute)
        button_disconnect.add_observer(button_start_image_acquisition)
        button_disconnect.add_observer(button_toggle_drawing)
        button_disconnect.add_observer(button_stop_image_acquisition)
        button_disconnect.add_observer(self._widget_device_list)

        #
        button_start_image_acquisition.add_observer(button_toggle_drawing)
        button_start_image_acquisition.add_observer(button_stop_image_acquisition)

        #
        button_toggle_drawing.add_observer(button_start_image_acquisition)
        button_toggle_drawing.add_observer(button_stop_image_acquisition)

        #
        button_stop_image_acquisition.add_observer(button_start_image_acquisition)
        button_stop_image_acquisition.add_observer(button_toggle_drawing)

        # Add buttons to groups:

        #
        group_gentl_info.addAction(button_select_file)
        group_gentl_info.addAction(button_update)

        #
        group_connection.addAction(button_connect)
        group_connection.addAction(button_disconnect)

        #
        group_device.addAction(button_start_image_acquisition)
        group_device.addAction(button_toggle_drawing)
        group_device.addAction(button_stop_image_acquisition)
        group_device.addAction(button_dev_attribute)

        #
        group_help.addAction(button_about)

        # Connect handler functions:

        #
        group_gentl_info.actionTriggered[QAction].connect(
            self.on_button_clicked_action
        )
        group_connection.actionTriggered[QAction].connect(
            self.on_button_clicked_action
        )
        group_device.actionTriggered[QAction].connect(
            self.on_button_clicked_action
        )
        group_display.actionTriggered[QAction].connect(
            self.on_button_clicked_action
        )
        group_help.actionTriggered[QAction].connect(
            self.on_button_clicked_action
        )

    @staticmethod
    def on_button_clicked_action(action):
        action.execute()

    @property
    def action_stop_image_acquisition(self):
        return self._action_stop_image_acquisition

    @property
    def ia(self):
        return self._ia

    @ia.setter
    def ia(self, value):
        self._ia = value

    def action_on_connect(self):
        #
        # The fetch worker runs unpaced; it blocks on the GenTL
        # new-buffer event wait by itself. See the thread implementation
        # for why the worker must not sleep between two fetches:
        #
        config = ParameterSet({
            ParameterKey.THREAD_FACTORY_METHOD: lambda: _PyQtThread(
                parent=self, mutex=self._mutex, update_cycle_us=0),
        })
        try:
            self._ia = self.harvester_core.create(
                self.device_list.currentIndex(), config=config)
        except Exception as e:
            # The GenTL producer may raise any error here, for example
            # when the device is already owned by another application.
            # Keep the GUI alive and tell the user; an exception that
            # escapes a Qt slot would abort the whole process:
            self._logger.error(e, exc_info=True)
            QMessageBox.critical(
                self, 'Connection failed',
                'Could not connect to the device.\n\n{}\n\n'
                'Hint: the device may already be opened by another '
                'application. Close it and try again.'.format(e))

        if not self._ia:
            # The device is not available.
            return

        self.ia.signal_stop_image_acquisition = self._signal_stop_image_acquisition

        try:
            if self.ia.remote_device.node_map:
                self._widget_attribute_controller = \
                    AttributeController(
                        self.ia.remote_device.node_map,
                        parent=self
                    )
        except AttributeError:
            pass

        self.canvas.ia = self.ia

        #
        # Size the acquisition buffer queue before the first start;
        # a small pool cannot absorb the stalls of the display loop:
        # while the canvas is busy building a frame on the main
        # thread, the producer runs out of free buffers and drops
        # frames, which caps the delivered frame rate at the display
        # cadence:
        #
        self._configure_buffers()

        # Reflect the effective buffer count on the toolbar control;
        # from here on the user can override the automatic policy:
        self._widget_buffer_count.set_minimum(
            max(1, self.ia.min_num_buffers))
        self._widget_buffer_count.set_value(self.ia.num_buffers)
        self._widget_buffer_count.setEnabled(True)

    def _configure_buffers(self):
        #
        # A queue of only a few buffers drops frames as soon as the
        # delivery or the scheduling stumbles for a moment, and the
        # frame rate collapses with it. Announce ten buffers by
        # default, as many more as the memory that is currently
        # available on the host allows, and fewer only when that
        # memory cannot pay for the default; the buffers are
        # allocated when the acquisition starts and are freed when it
        # stops. The policy itself is in compute_num_buffers and is
        # covered by unit tests.
        #
        ia = self.ia
        if ia is None:
            return

        try:
            payload_size = int(read_node_value(
                ia.remote_device.node_map, 'PayloadSize'))
        except (TypeError, ValueError):
            payload_size = 0

        # A deep pool actively harms the delivery at high frame rates:
        # hundreds of outstanding USB transfers degrade the producer's
        # submission path and the frames are lost in the wire. Keep the
        # automatic queue modest; the toolbar control overrides it:
        num_buffers = compute_num_buffers(
            payload_size, psutil.virtual_memory().available, maximum=30
        )
        try:
            # The GenTL producer may require more buffers than the
            # policy announces; its requirement wins:
            num_buffers = max(num_buffers, ia.min_num_buffers)
            ia.num_buffers = num_buffers
            self._logger.info(
                'configured {0} acquisition buffers '
                '(payload: {1} bytes, available memory: {2} bytes)'
                ''.format(
                    num_buffers, payload_size,
                    psutil.virtual_memory().available)
            )
            if self._debug_stats:
                print(
                    '[gui-stats] configured {0} acquisition buffers '
                    '(payload: {1} bytes)'.format(num_buffers, payload_size),
                    flush=True
                )
        except Exception as e:
            # Keep the producer's default rather than losing the
            # connection over a sizing disagreement:
            self._logger.warning(
                'failed to set the number of buffers: {}'.format(e)
            )

    def _on_num_buffers_changed(self, value):
        ia = self.ia
        if ia is None:
            return
        try:
            ia.num_buffers = value
            self._logger.info(
                'acquisition buffers set to {0}'.format(value))
        except Exception as e:
            # The producer rejected the value; restore the effective
            # one:
            self._logger.warning(
                'failed to set the number of buffers: {}'.format(e))
            self._widget_buffer_count.set_value(ia.num_buffers)

    def is_enabled_on_connect(self):
        enable = False
        if self.cti_files:
            if self.harvester_core.device_info_list:
                if self.ia is None:
                    enable = True
        return enable

    def action_on_disconnect(self):
        # The attribute controller is optional; ImageAcquirer destruction
        # must not depend on whether the device exposes a node map.
        if self._widget_attribute_controller:
            if self._widget_attribute_controller.isVisible():
                self._widget_attribute_controller.close()
            self._widget_attribute_controller = None

        if self.ia is not None:
            if self._thread_statistics_measurement.is_running():
                self._thread_statistics_measurement.stop()

            self.canvas.release_buffers()
            try:
                if self.ia.is_acquiring():
                    self.ia.stop()
            finally:
                image_acquirer = self.ia
                self.canvas.ia = None
                try:
                    image_acquirer.destroy()
                finally:
                    self._ia = None
                    self.canvas.pause_drawing(False)
                    self.update_statistics('')
                    self._widget_buffer_count.setEnabled(False)

    def action_on_select_file(self):
        # Show a dialog and update the CTI file list.
        dialog = QFileDialog(self)
        dialog.setWindowTitle('Select a CTI file to load')
        dialog.setNameFilter('CTI files (*.cti)')
        dialog.setFileMode(QFileDialog.ExistingFile)

        if dialog.exec_() == QDialog.Accepted:
            #
            file_path = dialog.selectedFiles()[0]

            #
            self.harvester_core.reset()

            # Update the path to the target GenTL Producer.
            self.harvester_core.add_file(file_path)

            # Update the device list.
            self.harvester_core.update()

            # A producer that fails to load is only logged by the core
            # library; tell the user instead of failing silently:
            if not self.harvester_core.device_info_list:
                QMessageBox.warning(
                    self, 'No device found',
                    'No device was found after loading the CTI file:\n'
                    '\n{}\n\n'
                    'Possible causes:\n'
                    '- The GenTL producer failed to load (missing VC++ '
                    'x64 redistributable, missing camera SDK/driver, or '
                    'the CTI file was copied without its sibling DLLs).\n'
                    '- The camera is not connected or not powered.'
                    ''.format(file_path))

    def is_enabled_on_select_file(self):
        enable = False
        if self.ia is None:
            enable = True
        return enable

    def action_on_update_list(self):
        self.harvester_core.update()

    def is_enabled_on_update_list(self):
        enable = False
        if self.cti_files:
            if self.ia is None:
                enable = True
        return enable

    def is_enabled_on_disconnect(self):
        enable = False
        if self.cti_files:
            if self.ia:
                enable = True
        return enable

    def action_on_start_image_acquisition(self):
        if self.ia.is_acquiring():
            # If it's pausing drawing images, just resume it and
            # immediately return this method.
            if self.canvas.is_pausing():
                self.canvas.resume_drawing()
        else:
            # Start a fresh statistics session and then start acquisition.
            self.ia.statistics.reset()
            self.canvas.reset_statistics()
            # Fetch buffers on the internal worker thread so that
            # acquisition is paced by the camera, not by the GUI display
            # loop; the canvas only consumes the internal buffer queue:
            self.ia.start(run_as_thread=True)
            self._thread_statistics_measurement.start()

            # The buffer count only takes effect at an acquisition
            # start; make the control read-only while acquiring:
            self._widget_buffer_count.setEnabled(False)

    def is_enabled_on_start_image_acquisition(self):
        enable = False
        if self.cti_files:
            if self.ia:
                if not self.ia.is_acquiring() or \
                        self.canvas.is_pausing():
                    enable = True
        return enable

    def action_on_stop_image_acquisition(self):
        if self.ia is None:
            return

        # Stop statistics measurement before stopping acquisition.
        if self._thread_statistics_measurement.is_running():
            self._thread_statistics_measurement.stop()

        # Release preserved buffers before stopping acquisition.
        self.canvas.release_buffers()
        self.ia.stop()

        # Restore normal drawing state.
        self.canvas.pause_drawing(False)

        # The buffers have been freed; the count is editable again:
        self._widget_buffer_count.setEnabled(True)

    def is_enabled_on_stop_image_acquisition(self):
        enable = False
        if self.cti_files:
            if self.ia:
                if self.ia.is_acquiring():
                    enable = True
        return enable

    def action_on_show_attribute_controller(self):
        if self.ia and self.attribute_controller.isHidden():
            self.attribute_controller.show()
            self.attribute_controller.expand_all()

    def is_enabled_on_show_attribute_controller(self):
        enable = False
        if self.cti_files:
            if self.ia is not None:
                enable = True
        return enable

    def action_on_toggle_drawing(self):
        self.canvas.toggle_drawing()

    def is_enabled_on_toggle_drawing(self):
        enable = False
        if self.cti_files:
            if self.ia:
                if self.ia.is_acquiring():
                    enable = True
        return enable

    def action_on_show_about(self):
        self.about.setModal(False)
        self.about.show()

    def _worker_update_statistics(self):
        ia = self.ia
        if ia is None:
            return

        node_map = ia.remote_device.node_map

        message_config = 'W: {0} x H: {1}, {2}, '.format(
            read_node_value(node_map, 'Width'),
            read_node_value(node_map, 'Height'),
            read_node_value(node_map, 'PixelFormat')
        )

        # Keep the original FPS statistic.
        display_fps = self.canvas.get_display_fps()

        # Acquisition FPS is the delivery rate measured on the
        # camera-paced worker thread, so it shows the actual delivery
        # rate regardless of the display loop:
        stats = ia.statistics
        acq_fps = stats.fps or 0.0
        min_acq_fps = stats.fps_min or 0.0
        max_acq_fps = stats.fps_max or 0.0

        #
        # Diagnostics for the acquisition path; enable it by setting
        # HARVESTER_STATS_DEBUG=1 before starting the GUI. One line per
        # tick tells whether the event thread is alive and how many
        # buffers it has delivered since the previous tick:
        #
        if self._debug_stats:
            thread = getattr(ia, '_event_new_buffer_thread', None)
            running = thread.is_running() if thread else 'n/a'
            num_images = stats.num_images
            previous = getattr(self, '_debug_previous_images', None)
            self._debug_previous_images = num_images
            delta = '?' if previous is None else '{0:d}'.format(
                num_images - previous
            )
            print(
                '[gui-stats] event_thread_running={0} '
                'images=+{1} ({2} total) fps={3:.1f} display_fps={4:.1f} '
                'draw[fetch={5:.1f}ms prepare={6:.1f}ms gl={7:.1f}ms '
                'n={8}]'
                ''.format(
                    running, delta, num_images, acq_fps, display_fps,
                    *self.canvas.get_draw_profile()
                ),
                flush=True
            )

        # Bandwidth estimated from the frame rate and the payload size,
        # in megabits per second:
        try:
            payload_size = int(read_node_value(node_map, 'PayloadSize'))
        except (TypeError, ValueError):
            payload_size = 0

        def bandwidth_mbps(fps):
            return fps * payload_size * 8.0 / 1000000.0

        bandwidth = bandwidth_mbps(acq_fps)
        min_bandwidth = bandwidth_mbps(min_acq_fps)
        max_bandwidth = bandwidth_mbps(max_acq_fps)

        elapsed = stats.elapsed_time_s
        num_images = stats.num_images

        # Frames that never made it to the host, counted at the same
        # site as the acquisition FPS through the gaps of the device
        # frame id; the buffers the internal display queue replaces
        # with newer frames are normal decimation, not lost frames:
        lost = stats.num_images_lost or 0

        # One line: the summary part and the detailed statistics part
        # are separated by a wide blank span:
        message_statistics = (
            'Display FPS: {0:.1f}, Elapsed: {1}, {2} Images, Lost: {3}'
            '          '
            'Acq. FPS: {4:.1f} (Min: {5:.1f}, Max: {6:.1f}), '
            'BW: {7:.2f} Mbps (Min: {8:.2f}, Max: {9:.2f})'
        ).format(
            display_fps,
            str(datetime.timedelta(seconds=int(elapsed))),
            num_images,
            lost,
            acq_fps,
            min_acq_fps,
            max_acq_fps,
            bandwidth,
            min_bandwidth,
            max_bandwidth
        )

        self._signal_update_statistics.emit(
            message_config + message_statistics
        )


class ActionSelectFile(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionUpdateList(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionConnect(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionDisconnect(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionStartImageAcquisition(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionToggleDrawing(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled,
            checkable=True
        )

    def _update(self):
        #
        checked = True if self.parent().canvas.is_pausing() else False
        self.setChecked(checked)


class ActionStopImageAcquisition(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionShowAttributeController(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )


class ActionShowAbout(Action):
    def __init__(
            self, icon=None, title=None, parent=None, action=None, is_enabled=None
    ):
        #
        super().__init__(
            icon=icon, title=title, parent=parent, action=action, is_enabled=is_enabled
        )

        #
        self._is_model = False


if __name__ == '__main__':
    app = QApplication(sys.argv)
    harvester = Harvester(vsync=True)
    harvester.show()
    sys.exit(app.exec_())
