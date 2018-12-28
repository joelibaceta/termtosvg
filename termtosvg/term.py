import datetime
import fcntl
import os
import pty
import select
import struct
import termios
import tty
from copy import copy
from collections import namedtuple
from typing import Iterator

import pyte
import pyte.screens

from termtosvg import anim
from termtosvg.asciicast import AsciiCastV2Event, AsciiCastV2Header


class TerminalMode:
    """Save terminal mode and size on entry, restore them on exit"""
    def __init__(self, fileno):
        self.fileno = fileno
        self.mode = None
        self.ttysize = None

    def __enter__(self):
        try:
            self.mode = tty.tcgetattr(self.fileno)
        except tty.error:
            pass

        try:
            columns, lines = os.get_terminal_size(self.fileno)
        except OSError:
            pass
        else:
            self.ttysize = struct.pack("HHHH", lines, columns, 0, 0)

        return self.mode, self.ttysize

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ttysize is not None:
            fcntl.ioctl(self.fileno, termios.TIOCSWINSZ, self.ttysize)

        if self.mode is not None:
            tty.tcsetattr(self.fileno, tty.TCSAFLUSH, self.mode)


def record(process_args, columns, lines, input_fileno, output_fileno):
    """Record a process in asciicast v2 format

    The records returned are of two types:
        - a single header with configuration information
        - multiple event records with data captured from the terminal and timing information
    """
    yield AsciiCastV2Header(version=2, width=columns, height=lines, theme=None)

    start = None
    for data, time in _record(process_args, columns, lines, input_fileno, output_fileno):
        if start is None:
            start = time

        yield AsciiCastV2Event(time=(time - start).total_seconds(),
                               event_type='o',
                               event_data=data,
                               duration=None)


def _record(process_args, columns, lines, input_fileno, output_fileno):
    """Record raw input and output of a process

    This function forks the current process. The child process runs the command specified by
    'process_args' which is a session leader and has a controlling terminal and is run in the
    background. The parent process, which runs in the foreground, transmits data between the
    standard input, output and the child process and logs it. From the user point of view, it
    appears they are communicating with the process they intend to record (through their terminal
    emulator) when in fact they communicate with our parent process which logs all data exchanges
    with the user

    The implementation of this method is mostly copied from the pty.spawn function of the
    CPython standard library. It has been modified in order to make the record function a
    generator.
    See https://github.com/python/cpython/blob/master/Lib/pty.py

    :param process_args: List of arguments to run the process to be recorded
    :param columns: Initial number of columns of the terminal
    :param lines: Initial number of lines of the terminal
    :param input_fileno: File descriptor of the input data stream
    :param output_fileno: File descriptor of the output data stream
    """
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child process - this call never returns
        os.execlp(process_args[0], *process_args)

    # Parent process
    # Set the terminal size for master_fd
    ttysize = struct.pack("HHHH", lines, columns, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ttysize)

    try:
        tty.setraw(input_fileno)
    except tty.error:
        pass

    for data, time in _capture_data(input_fileno, output_fileno, master_fd):
        yield data, time

    os.close(master_fd)

    _, child_exit_status = os.waitpid(pid, 0)
    return child_exit_status


def _capture_data(input_fileno, output_fileno, master_fd, buffer_size=1024):
    """Send data from input_fileno to master_fd and send data from master_fd to output_fileno and
    also return it to the caller

    The implementation of this method is mostly copied from the pty.spawn function of the
    CPython standard library. It has been modified in order to make the record function a
    generator.
    See https://github.com/python/cpython/blob/master/Lib/pty.py
    """
    rlist = [input_fileno, master_fd]
    xlist = [input_fileno, output_fileno, master_fd]

    xfds = []
    while not xfds:
        rfds, _, xfds = select.select(rlist, [], xlist)
        for fd in rfds:
            try:
                data = os.read(fd, buffer_size)
            except OSError:
                xfds.append(fd)
                continue

            if not data:
                xfds.append(fd)
                continue

            if fd == input_fileno:
                write_fileno = master_fd
            else:
                write_fileno = output_fileno
                yield data, datetime.datetime.now()

            while data:
                n = os.write(write_fileno, data)
                data = data[n:]


def _group_by_time(event_records, min_rec_duration, max_rec_duration, last_rec_duration):
    """Merge event records together if they are close enough and compute the duration between
    consecutive events. The duration between two consecutive event records returned by the function
    is guaranteed to be at least min_rec_duration.

    :param event_records: Sequence of records in asciicast v2 format
    :param min_rec_duration: Minimum time between two records returned by the function in
    milliseconds. This helps avoiding 0s duration animations which break SVG animations.
    :param max_rec_duration: Limit of the time elapsed between two records
    :param last_rec_duration: Duration of the last record in milliseconds
    :return: Sequence of records
    """
    current_string = b''
    current_time = 0
    dropped_time = 0

    if max_rec_duration:
        max_rec_duration /= 1000

    for event_record in event_records:
        assert isinstance(event_record, AsciiCastV2Event)
        if event_record.event_type != 'o':
            continue

        time_between_events = event_record.time - (current_time + dropped_time)
        if time_between_events * 1000 >= min_rec_duration:
            if max_rec_duration:
                if max_rec_duration < time_between_events:
                    dropped_time += time_between_events - max_rec_duration
                    time_between_events = max_rec_duration
            accumulator_event = AsciiCastV2Event(time=current_time,
                                                 event_type='o',
                                                 event_data=current_string,
                                                 duration=time_between_events)
            yield accumulator_event
            current_string = b''
            current_time += time_between_events

        current_string += event_record.event_data

    if current_string:
        accumulator_event = AsciiCastV2Event(time=current_time,
                                             event_type='o',
                                             event_data=current_string,
                                             duration=last_rec_duration / 1000)
        yield accumulator_event


class TerminalSession:
    Configuration = namedtuple('Configuration', ['width', 'height'])
    DisplayLine = namedtuple('DisplayLine', ['row', 'line', 'time', 'duration'])
    DisplayLine.__new__.__defaults__ = (None,)

    def __init__(self, records):
        if isinstance(records, Iterator):
            self._asciicast_records = records
        else:
            self._asciicast_records = iter(records)

    @classmethod
    def from_process(cls, process_args, columns, lines, input_fileno, output_fileno):
        records = record(process_args, columns, lines, input_fileno, output_fileno)
        return cls(records)

    def line_events(self, min_frame_dur=1, max_frame_dur=None, last_frame_dur=1000):
        header = next(self._asciicast_records)
        assert isinstance(header, AsciiCastV2Header)

        if not max_frame_dur and header.idle_time_limit:
            max_frame_dur = int(header.idle_time_limit * 1000)
        yield self.Configuration(header.width, header.height)

        screen = pyte.Screen(header.width, header.height)
        stream = pyte.ByteStream(screen)

        timed_records = _group_by_time(self._asciicast_records, min_frame_dur,
                                       max_frame_dur, last_frame_dur)
        last_cursor = None
        display_events = {}
        time = 0
        for record_ in timed_records:
            assert isinstance(record_, AsciiCastV2Event)
            stream.feed(record_.event_data)
            last_cursor, display_events, events = self._feed(screen, last_cursor, display_events,
                                                             time)
            for event in events:
                yield event

            screen.dirty.clear()
            time += int(1000 * record_.duration)

        for row in list(display_events):
            event_without_duration = display_events.pop(row)
            duration = time - event_without_duration.time
            yield event_without_duration._replace(duration=duration)

    @classmethod
    def _buffer(cls, screen, last_cursor):
        """Return lines of the screen to be redrawn"""
        assert isinstance(screen, pyte.Screen)
        assert isinstance(last_cursor, (type(None), pyte.screens.Cursor))

        rows_changed = set(screen.dirty)
        if screen.cursor != last_cursor:
            if not screen.cursor.hidden:
                rows_changed.add(screen.cursor.y)
            if last_cursor is not None and not last_cursor.hidden:
                rows_changed.add(last_cursor.y)

        redraw_buffer = {}
        for row in rows_changed:
            line = {
                column: anim.CharacterCell.from_pyte(screen.buffer[row][column])
                for column in screen.buffer[row]
            }
            if line:
                redraw_buffer[row] = line

        if (screen.cursor != last_cursor and
                not screen.cursor.hidden):
            row, column = screen.cursor.y, screen.cursor.x
            try:
                data = screen.buffer[row][column].data
            except KeyError:
                data = ' '

            cursor_char = pyte.screens.Char(data=data,
                                            fg=screen.cursor.attrs.fg,
                                            bg=screen.cursor.attrs.bg,
                                            reverse=True)
            if row not in redraw_buffer:
                redraw_buffer[row] = {}
            redraw_buffer[row][column] = anim.CharacterCell.from_pyte(cursor_char)

        last_cursor = copy(screen.cursor)
        return redraw_buffer, last_cursor

    @classmethod
    def _feed(cls, screen, last_cursor, display_events, time):
        """Update terminal session from asciicast V2 event record"""
        assert isinstance(screen, pyte.Screen)
        assert isinstance(last_cursor, (type(None), pyte.screens.Cursor))
        events = []
        redraw_buffer, last_cursor = cls._buffer(screen, last_cursor)

        # Send TerminalDisplayDuration event for old lines that were
        # displayed on the screen and need to be redrawn
        for row in list(display_events):
            if row in redraw_buffer:
                event_without_duration = display_events.pop(row)
                duration = time - event_without_duration.time
                events.append(event_without_duration._replace(duration=duration))

        # Send TerminalDisplayLine event for non empty new (or updated) lines
        for row in redraw_buffer:
            if redraw_buffer[row]:
                display_events[row] = cls.DisplayLine(row, redraw_buffer[row],
                                                      time, None)
                events.append(display_events[row])

        return last_cursor, display_events, events


# def replay(records, from_pyte_char, min_frame_duration, max_frame_duration, last_frame_duration=1000):
#     """Read the records of a terminal sessions, render the corresponding screens and return lines
#     of the screen that need updating.
#
#     Records are merged together so that there is at least a 'min_frame_duration' seconds pause
#     between two rendered screens.
#     Lines returned are sorted by time and duration of their appearance on the screen so that lines
#     in need of updating at the same time can easily be grouped together.
#     The terminal screen is rendered using Pyte and then each character of the screen is converted
#     to the caller's format of choice using from_pyte_char
#
#     :param records: Records of the terminal session in asciicast v2 format. The first record must
#     be a header, which must be followed by event records.
#     :param from_pyte_char: Conversion function from pyte.screen.Char to any other format
#     :param min_frame_duration: Minimum frame duration in milliseconds. SVG animations break when
#     an animation duration is 0ms so setting this to at least 1ms is recommended.
#     :param max_frame_duration: Maximum duration of a frame in milliseconds. This is meant to limit
#     idle time during a recording.
#     :param last_frame_duration: Last frame duration in milliseconds
#     :return: Records in the CharacterCellRecord format:
#         1/ a header with configuration information (CharacterCellConfig)
#         2/ one event record for each line of the screen that need to be redrawn
#         (CharacterCellLineEvent)
#     """


def get_terminal_size(fileno):
    try:
        columns, lines = os.get_terminal_size(fileno)
    except OSError:
        columns, lines = 80, 24

    return columns, lines
