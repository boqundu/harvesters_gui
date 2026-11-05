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

# Related third party imports

import time
import threading

import numpy as np

from vispy import gloo
from vispy import app
from vispy.gloo import Program
from vispy.util.transforms import ortho

from genicam.gentl import PAYLOADTYPE_INFO_IDS
from genicam.gentl import TimeoutException

# Local application/library specific imports
from harvesters._private.core.helper.system import is_running_on_macos
from harvesters.util.pfnc import is_custom, get_bits_per_pixel, \
    bgr_formats
from harvesters.util.pfnc import mono_location_formats, \
    rgb_formats, bgr_formats, \
    rgba_formats, bgra_formats, \
    bayer_location_formats
from harvesters.util.bayer import cfa_positions, demosaic


class CanvasBase(app.Canvas):
    def __init__(
            self, *,
            image_acquirer=None,
            width=640, height=480,
            display_rate=60.,
            background_color='gray',
            vsync=True
    ):
        """
        As far as we know, Vispy refreshes the canvas every 1/30 sec at the
        fastest no matter which faster number is specified. If we set any
        value which is greater than 30, then Vispy's callback is randomly
        called.
        """

        #
        app.Canvas.__init__(
            self, size=(width, height), vsync=vsync, autoswap=True
        )

        #
        self._ia = image_acquirer

        #
        self._background_color = background_color
        self._has_filled_texture = False
        self._width, self._height = width, height

        #
        self._is_dragging = False

        # If it's True, the canvas keeps image acquisition but do not
        # draw images on the canvas:
        self._pause_drawing = False

        #
        self._origin = [0, 0]

        #
        self._display_rate = display_rate
        self._timer = app.Timer(
            1. / self._display_rate, connect=self.update, start=True
        )

        #
        self._buffers = []

        # Acquisition statistics are measured from buffers fetched from the
        # ImageAcquirer, independently of the canvas refresh rate.
        self._statistics_lock = threading.Lock()
        self._statistics_last_time = None
        self._statistics_interval_frames = 0
        self._statistics_interval_bytes = 0
        self._statistics_fps = 0.0
        self._statistics_bandwidth = 0.0
        self._statistics_min_fps = None
        self._statistics_max_fps = None
        self._statistics_min_bandwidth = None
        self._statistics_max_bandwidth = None
        # Display FPS statistics.
        self._display_last_time = None
        self._display_interval_frames = 0
        self._display_fps = 0.0
        # Fit the complete image into the available canvas until the user
        # changes magnification manually.
        self._fit_to_window = True
        self._user_zoom = 1.0

    @property
    def display_rate(self):
        return self._display_rate

    @display_rate.setter
    def display_rate(self, value):
        self._display_rate = value
        self._timer.stop()
        self._timer.start(interval=1./self._display_rate)

    def set_canvas_size(self, width, height):
        #
        self._has_filled_texture = False

        #
        updated = False

        #
        if self._width != width or self._height != height:
            self._width = width
            self._height = height
            updated = True

        #
        if updated:
            self.apply_magnification()

    def reset_statistics(self):
        now = time.perf_counter()

        with self._statistics_lock:
            # Acquisition statistics.
            self._statistics_last_time = now
            self._statistics_interval_frames = 0
            self._statistics_interval_bytes = 0
            self._statistics_fps = None
            self._statistics_bandwidth = None
            self._statistics_min_fps = None
            self._statistics_max_fps = None
            self._statistics_min_bandwidth = None
            self._statistics_max_bandwidth = None

            # Display statistics.
            self._display_last_time = now
            self._display_interval_frames = 0
            self._display_fps = None

    @staticmethod
    def _get_buffer_payload_size(buffer):
        """Return the number of image payload bytes represented by a buffer."""
        payload = getattr(buffer, 'payload', None)
        if payload is None:
            return 0

        # Prefer a producer-reported payload size when available.
        for owner in (buffer, payload):
            for name in ('payload_size', 'size'):
                value = getattr(owner, name, None)
                if isinstance(value, (int, np.integer)) and value >= 0:
                    return int(value)

        # Fall back to the actual component arrays. This also supports
        # multipart payloads.
        raw_buffer = getattr(payload, 'raw_buffer', None)
        if raw_buffer is not None:
            try:
                return int(np.asarray(raw_buffer).nbytes)
            except (TypeError, ValueError):
                pass

        total = 0
        for component in getattr(payload, 'components', ()):
            data = getattr(component, 'data', None)
            if data is not None:
                total += int(np.asarray(data).nbytes)
        return total

    def _record_acquired_buffer(self, buffer):
        now = time.perf_counter()
        payload_size = self._get_buffer_payload_size(buffer)
        with self._statistics_lock:
            self._statistics_interval_frames += 1
            self._statistics_interval_bytes += payload_size
            if self._statistics_last_time is None:
                self._statistics_last_time = now

    def get_statistics(self):
        """Return (instant fps, min fps, max fps, MB/s, min MB/s, max MB/s)."""
        now = time.perf_counter()

        with self._statistics_lock:
            if self._statistics_last_time is None:
                self._statistics_last_time = now

            elapsed = now - self._statistics_last_time

            if elapsed > 0 and self._statistics_interval_frames > 0:
                fps = self._statistics_interval_frames / elapsed
                bandwidth = (
                    self._statistics_interval_bytes / elapsed / 1000000.0
                )

                self._statistics_fps = fps
                self._statistics_bandwidth = bandwidth

                # Initialize Min/Max with the first valid sample.
                if self._statistics_min_fps is None:
                    self._statistics_min_fps = fps
                    self._statistics_max_fps = fps
                    self._statistics_min_bandwidth = bandwidth
                    self._statistics_max_bandwidth = bandwidth
                else:
                    self._statistics_min_fps = min(
                        self._statistics_min_fps, fps
                    )
                    self._statistics_max_fps = max(
                        self._statistics_max_fps, fps
                    )
                    self._statistics_min_bandwidth = min(
                        self._statistics_min_bandwidth, bandwidth
                    )
                    self._statistics_max_bandwidth = max(
                        self._statistics_max_bandwidth, bandwidth
                    )

                # Reset the sampling interval.
                self._statistics_interval_frames = 0
                self._statistics_interval_bytes = 0
                self._statistics_last_time = now

            return (
                self._statistics_fps or 0.0,
                self._statistics_min_fps or 0.0,
                self._statistics_max_fps or 0.0,
                self._statistics_bandwidth or 0.0,
                self._statistics_min_bandwidth or 0.0,
                self._statistics_max_bandwidth or 0.0,
            )

    def _record_displayed_frame(self):
        """Record one successfully displayed image."""
        with self._statistics_lock:
            self._display_interval_frames += 1

    def get_display_fps(self):
        """Return the instantaneous GUI display frame rate."""
        now = time.perf_counter()

        with self._statistics_lock:
            if self._display_last_time is None:
                self._display_last_time = now
                return 0.0

            elapsed = now - self._display_last_time

            if elapsed > 0:
                self._display_fps = (
                    self._display_interval_frames / elapsed
                )

                self._display_interval_frames = 0
                self._display_last_time = now

            return self._display_fps

    def on_draw(self, event):
        # Update on June 15th, 2018:
        # According to a VisPy developer, they have not finished
        # porting VisPy to PyQt5. Once they finished the development
        # we should try it out if it gives us the maximum refresh rate.
        # See the following URL to check the latest information:
        #
        #     https://github.com/vispy/vispy/issues/1394

        # Clear the canvas in gray.
        gloo.clear(color=self._background_color)

        drew = False
        latest_buffer = None

        if self._pause_drawing:
            # The previously displayed buffer is no longer needed while
            # drawing is paused.
            self.release_buffers()

        # Drain all buffers currently available. Otherwise a 30/60 Hz display
        # loop can silently limit the measured/consumed frame rate of a faster
        # camera and let the input queue grow.
        try:
            while self.ia is not None:
                try:
                    buffer = self.ia.fetch(timeout=0.0001)
                except TimeoutException:
                    break

                self._record_acquired_buffer(buffer)

                if self._pause_drawing:
                    buffer.queue()
                    continue

                # Keep only the newest frame for display. Older buffers are
                # queued immediately so that they cannot exhaust the pool.
                if latest_buffer is not None:
                    latest_buffer.queue()
                latest_buffer = buffer

        except AttributeError:
            # No ImageAcquirer is connected.
            pass

        if latest_buffer is not None:
            try:
                self._prepare_texture(latest_buffer)
                self._draw()
                drew = True
                # Count only successfully displayed images.
                self._record_displayed_frame()
            except Exception:
                latest_buffer.queue()
                raise
            else:
                # Retain the displayed buffer until the next frame so chunk
                # data remains valid for the texture's lifetime.
                self.release_buffers()
                self._buffers.append(latest_buffer)

        if not drew:
            self._draw()

    def release_buffers(self):
        for _buffer in self._buffers:
            if _buffer:
                _buffer.queue()
        self._buffers.clear()

    def _draw(self):
        raise NotImplementedError

    def on_resize(self, event):
        self.apply_magnification()

    def apply_magnification(self):
        raise NotImplementedError

    def on_mouse_wheel(self, event):
        raise NotImplementedError

    def on_mouse_press(self, event):
        self._is_dragging = True
        self._origin = event.pos

    def on_mouse_release(self, event):
        self._is_dragging = False

    def on_mouse_move(self, event):
        raise NotImplementedError

    def pause_drawing(self, pause=True):
        self._pause_drawing = pause

    def toggle_drawing(self):
        self._pause_drawing = False if self._pause_drawing else True

    def is_pausing(self):
        return True if self._pause_drawing else False

    def resume_drawing(self):
        self._pause_drawing = False

    @property
    def background_color(self):
        return self._background_color

    @background_color.setter
    def background_color(self, color):
        self._background_color = color

    @property
    def ia(self):
        return self._ia

    @ia.setter
    def ia(self, value):
        self._ia = value

    def _prepare_texture(self, buffer):
        raise NotImplementedError


class Canvas2D(CanvasBase):
    _visible_payloads = [
        PAYLOADTYPE_INFO_IDS.PAYLOAD_TYPE_IMAGE,
        PAYLOADTYPE_INFO_IDS.PAYLOAD_TYPE_CHUNK_DATA,
        PAYLOADTYPE_INFO_IDS.PAYLOAD_TYPE_MULTI_PART,
    ]
    # Also draw GenDC payloads; genicam < 1.6 does not define the type:
    _visible_payloads.extend(
        v for v in (
            getattr(PAYLOADTYPE_INFO_IDS, 'PAYLOAD_TYPE_GENDC', None),)
        if v is not None)

    def __init__(
            self, *,
            image_acquirer=None,
            width=640, height=480,
            background_color='gray',
            vsync=True, display_rate=60.
    ):
        #
        super().__init__(
            image_acquirer=image_acquirer,
            width=width, height=height,
            display_rate=display_rate,
            background_color=background_color,
            vsync=vsync
        )

        #
        self._vertex_shader = """
            // Uniforms
            uniform mat4 u_model;
            uniform mat4 u_view;
            uniform mat4 u_projection;

            // Attributes
            attribute vec2 a_position;
            attribute vec2 a_texcoord;

            // Varyings
            varying vec2 v_texcoord;

            // Main
            void main (void)
            {
                v_texcoord = a_texcoord;
                gl_Position = u_projection * u_view * u_model * vec4(a_position, 0.0, 1.0);
            }
        """

        self._fragment_shader = """
            varying vec2 v_texcoord;
            uniform sampler2D texture;

            // Bayer CFA demosaicing on the GPU; enabled per frame:
            uniform bool u_demosaic;
            uniform vec2 u_size;
            uniform ivec2 u_red_pos;
            uniform ivec2 u_blue_pos;

            float tap(vec2 t)
            {
                // Sample the exact center of texel t; the texture uses
                // edge clamping so the border replicates its edges:
                return texture2D(texture, (t + 0.5) / u_size).r;
            }

            void main()
            {
                if (!u_demosaic) {
                    gl_FragColor = texture2D(texture, v_texcoord);
                    return;
                }

                // The texel that this fragment is about to cover:
                vec2 texel = floor(v_texcoord * u_size);
                float c = tap(texel);
                float l = tap(texel + vec2(-1.0, 0.0));
                float r = tap(texel + vec2( 1.0, 0.0));
                float u = tap(texel + vec2(0.0, -1.0));
                float d = tap(texel + vec2(0.0,  1.0));
                float ul = tap(texel + vec2(-1.0, -1.0));
                float ur = tap(texel + vec2( 1.0, -1.0));
                float dl = tap(texel + vec2(-1.0,  1.0));
                float dr = tap(texel + vec2( 1.0,  1.0));
                ivec2 ip = ivec2(mod(texel, 2.0));

                float rr;
                if (ip == u_red_pos) { rr = c; }
                else if (ip.y == u_red_pos.y) { rr = (l + r) * 0.5; }
                else if (ip.x == u_red_pos.x) { rr = (u + d) * 0.5; }
                else { rr = (ul + ur + dl + dr) * 0.25; }

                float bb;
                if (ip == u_blue_pos) { bb = c; }
                else if (ip.y == u_blue_pos.y) { bb = (l + r) * 0.5; }
                else if (ip.x == u_blue_pos.x) { bb = (u + d) * 0.5; }
                else { bb = (ul + ur + dl + dr) * 0.25; }

                float gg;
                if (ip == u_red_pos || ip == u_blue_pos) {
                    gg = (l + r + u + d) * 0.25;
                } else { gg = c; }

                gl_FragColor = vec4(rr, gg, bb, 1.0);
            }
        """

        #
        self._program = None
        self._data = None
        self._coordinate = None
        self._translate = 0.
        self._latest_translate = self._translate
        self._magnification = 1.

        # Apply shaders.
        self._program = Program(
            self._vertex_shader, self._fragment_shader, count=4
        )

        #
        self._data = np.zeros(
            4, dtype=[
                ('a_position', np.float32, 2),
                ('a_texcoord', np.float32, 2)
            ]
        )

        #
        self._data['a_texcoord'] = np.array(
            [[0., 1.], [1., 1.], [0., 0.], [1., 0.]]
        )

        #
        self._program['u_model'] = np.eye(4, dtype=np.float32)
        self._program['u_view'] = np.eye(4, dtype=np.float32)

        #
        self._coordinate = [0, 0]


        #
        self._program['texture'] = np.zeros(
            (self._height, self._width), dtype='uint8'
        )

        # Demosaicing is off until a Bayer image is uploaded:
        self._program['u_demosaic'] = False
        self._program['u_size'] = (self._width, self._height)
        self._program['u_red_pos'] = (0, 0)
        self._program['u_blue_pos'] = (1, 1)

        #
        self.apply_magnification()

    def _prepare_texture(self, buffer):
        if not buffer or buffer.payload_type not in self._visible_payloads:
            return

        # Some payload types (for example chunk-only data) may not contain an
        # image component. Do not index an empty component list.
        payload = buffer.payload
        components = getattr(payload, 'components', None)
        if not components:
            return

        update = True
        component = components[0]
        width = component.width
        height = component.height

        # Update the canvas size if needed.
        self.set_canvas_size(width, height)

        #
        exponent = 0
        data_format = None

        #
        data_format_value = component.data_format_value
        if is_custom(data_format_value):
            update = False
        else:
            data_format = component.data_format
            bpp = get_bits_per_pixel(data_format)
            if bpp is not None:
                exponent = bpp - 8
            else:
                update = False

        if update:
            # Reshape the image so that it can be drawn on the
            # VisPy canvas:
            if data_format in mono_location_formats or \
                    data_format in bayer_location_formats:
                # Reshape the 1D NumPy array into a 2D so that VisPy
                # can display it as a mono image:
                content = component.data.reshape(height, width)
            else:
                # The image requires you to reshape it to draw it on the
                # canvas:
                if data_format in rgb_formats or \
                        data_format in rgba_formats or \
                        data_format in bgr_formats or \
                        data_format in bgra_formats:
                    # Reshape the 1D NumPy array into a 2D so that VisPy
                    # can display it as an RGB image:
                    content = component.data.reshape(
                        height, width,
                        int(component.num_components_per_pixel)
                    )
                    #
                    if data_format in bgr_formats:
                        # Swap red and blue channels.
                        content = content[:, :, [2, 1, 0]]
                    elif data_format in bgra_formats:
                        # Swap red and blue while keeping alpha in place.
                        content = content[:, :, [2, 1, 0, 3]]
                else:
                    return

            # Convert each data to an 8bit.
            if exponent > 0:
                # The following code may affect to the rendering
                # performance:
                content = (content / (2 ** exponent))

                # Then cast each array element to an uint8:
                content = content.astype(np.uint8)

            if data_format in bayer_location_formats and exponent == 0:
                # Upload the raw single-channel CFA image and let the
                # GPU demosaic it per output pixel while rendering: the
                # upload costs a third of an RGB image and the CPU is
                # freed from the demosaicing work, keeping the display
                # at the acquisition frame rate:
                red_pos, blue_pos = cfa_positions(data_format)
                self._program['u_demosaic'] = True
                self._program['u_size'] = (width, height)
                self._program['u_red_pos'] = red_pos
                self._program['u_blue_pos'] = blue_pos
                self._set_texture_interpolation('nearest')
            else:
                self._program['u_demosaic'] = False
                self._set_texture_interpolation('linear')
                if data_format in bayer_location_formats:
                    # A more-than-8-bit Bayer image is demosaiced on
                    # the CPU after the 8-bit conversion; the GPU path
                    # above only handles 8-bit inputs:
                    content = demosaic(content, data_format)

            self._program['texture'] = content

    def _set_texture_interpolation(self, mode):
        #
        try:
            self._program['texture'].interpolation = mode
        except (AttributeError, KeyError, TypeError):
            # The shader samples exact texel centers so even without
            # switching the interpolation the demosaicing stays exact:
            pass

    def _draw(self):
        self._program.draw('triangle_strip')

    def apply_magnification(self):
        #
        canvas_w, canvas_h = self.physical_size
        gloo.set_viewport(0, 0, canvas_w, canvas_h)

        #
        w, h = self._width, self._height
        if w <= 0 or h <= 0 or canvas_w <= 0 or canvas_h <= 0:
            return

        if self._fit_to_window:
            fit_scale = min(float(canvas_w) / w, float(canvas_h) / h)
            ratio = 1.0 / fit_scale
        else:
            fit_scale = min(float(canvas_w) / w, float(canvas_h) / h)
            ratio = 1.0 / (fit_scale * self._user_zoom)

        self._magnification = ratio
        self._program['u_projection'] = ortho(
            self._coordinate[0],
            canvas_w * ratio + self._coordinate[0],
            self._coordinate[1],
            canvas_h * ratio + self._coordinate[1],
            -1, 1
        )

        x = int((canvas_w * ratio - w) / 2)
        y = int((canvas_h * ratio - h) / 2)

        #
        self._data['a_position'] = np.array(
            [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]
        )

        #
        self._program.bind(gloo.VertexBuffer(self._data))

    def on_mouse_wheel(self, event):
        self._translate += event.delta[1]
        power = 7. if is_running_on_macos() else 5.
        stride = 4. if is_running_on_macos() else 7.
        translate = min(power * stride, self._translate)
        translate = max(-power * stride, translate)
        self._translate = translate
        self._user_zoom = 2 ** (self._translate / stride)
        self._fit_to_window = False
        if self._latest_translate != self._translate:
            self.apply_magnification()
            self._latest_translate = self._translate

    def on_mouse_move(self, event):
        if self._is_dragging:
            adjustment = 2. if is_running_on_macos() else 1.
            ratio = self._magnification * adjustment
            delta = event.pos - self._origin
            self._origin = event.pos
            self._coordinate[0] -= (delta[0] * ratio)
            self._coordinate[1] += (delta[1] * ratio)
            self.apply_magnification()
