import os
import time
import unittest
from unittest.mock import MagicMock, patch

import pyte

import termtosvg.anim as anim
from termtosvg import term
from termtosvg.asciicast import AsciiCastV2Event, AsciiCastV2Header, AsciiCastV2Theme

commands = [
    'echo $SHELL && sleep 0.1;\r\n',
    'date && sleep 0.1;\r\n',
    'uname && sleep 0.1;\r\n',
    'w',
    'h',
    'o',
    'a',
    'm',
    'i\r\n',
    'exit;\r\n'
]


class TestTerm(unittest.TestCase):
    maxDiff = None

    def test__record(self):
        # Use pipes in lieu of stdin and stdout
        fd_in_read, fd_in_write = os.pipe()
        fd_out_read, fd_out_write = os.pipe()

        lines = 24
        columns = 80

        pid = os.fork()
        if pid == 0:
            # Child process
            for line in commands:
                os.write(fd_in_write, line.encode('utf-8'))
                time.sleep(0.060)
            os._exit(0)

        # Parent process
        with term.TerminalMode(fd_in_read):
            for _ in term._record(['sh'], columns, lines, fd_in_read, fd_out_write):
                pass

        os.waitpid(pid, 0)
        for fd in fd_in_read, fd_in_write, fd_out_read, fd_out_write:
            os.close(fd)

    def test_record(self):
        # Use pipes in lieu of stdin and stdout
        fd_in_read, fd_in_write = os.pipe()
        fd_out_read, fd_out_write = os.pipe()

        lines = 24
        columns = 80

        pid = os.fork()
        if pid == 0:
            # Child process
            for line in commands:
                os.write(fd_in_write, line.encode('utf-8'))
                time.sleep(0.060)
            os._exit(0)

        # Parent process
        with term.TerminalMode(fd_in_read):
            for _ in term.record(['sh'], columns, lines, fd_in_read, fd_out_write):
                pass

        os.waitpid(pid, 0)
        for fd in fd_in_read, fd_in_write, fd_out_read, fd_out_write:
            os.close(fd)

    def test_TerminalSession__buffer(self):
        records = [AsciiCastV2Event(time=i,
                                    event_type='o',
                                    event_data='{}\r\n'.format(i).encode('utf-8'),
                                    duration=None)
                   for i in range(1, 5)]

        screen = pyte.Screen(80, 24)
        stream = pyte.ByteStream(screen)
        last_cursor = None
        for count, record in enumerate(records):
            with self.subTest(case='Simple events (record #{})'.format(count)):
                stream.feed(record.event_data)
                buffer, last_cursor = term.TerminalSession._buffer(screen,
                                                                   last_cursor)
                screen.dirty.clear()

                self.assertEqual(len(buffer), 2)
                # text from event data
                self.assertEqual(len(buffer[count]), 1)
                self.assertEqual(buffer[count][0].text, str(count+1))
                # cursor
                self.assertEqual(len(buffer[count+1]), 1)
                self.assertEqual(buffer[count+1][0].text, ' ')

    def test_TerminalSession__feed(self):
        records = [AsciiCastV2Event(time=i*1000,
                                    event_type='o',
                                    event_data='{}\r\n'.format(i).encode('utf-8'),
                                    duration=1)
                   for i in range(1, 3)]

        cursor_char = anim.CharacterCell(' ',
                                         color='background',
                                         background_color='foreground')
        expected_events = [
            term.TerminalSession.DisplayLine(0, {0: anim.CharacterCell('1')}, 0),
            term.TerminalSession.DisplayLine(1, {0: cursor_char}, 0),
            term.TerminalSession.DisplayLine(1, {0: cursor_char}, 0, 1000),
            term.TerminalSession.DisplayLine(1, {0: anim.CharacterCell('2')}, 1000),
            term.TerminalSession.DisplayLine(2, {0: cursor_char}, 1000),
        ]

        screen = pyte.Screen(80, 24)
        stream = pyte.ByteStream(screen)
        last_cursor = None
        display_events = {}
        time = 0

        events = []
        for record in records:
            stream.feed(record.event_data)
            last_cursor, display_events, record_events = term.TerminalSession._feed(
                screen, last_cursor, display_events, time
            )

            events.extend(record_events)
            screen.dirty.clear()
            time += int(1000 * record.duration)

        self.assertEqual(expected_events, events)

    def test_TerminalSession_line_events(self):
        cursor_char = anim.CharacterCell(' ', 'background', 'foreground')
        theme = AsciiCastV2Theme('#000000', '#FFFFFF', ':'.join(['#123456'] * 16))

        with self.subTest(case='Simple events'):
            records = [AsciiCastV2Header(version=2, width=80, height=24, theme=theme)] + \
                      [AsciiCastV2Event(time=i,
                                        event_type='o',
                                        event_data='{}\r\n'.format(i).encode('utf-8'),
                                        duration=1)
                       for i in range(0, 2)]
            session = term.TerminalSession(records)
            events = list(session.line_events(1, None, 42))
            expected_events = [
                session.Configuration(80, 24),
                session.DisplayLine(0, {0: anim.CharacterCell('0')}, 0),
                session.DisplayLine(1, {0: cursor_char}, 0),
                session.DisplayLine(1, {0: cursor_char}, 0, 1000),
                session.DisplayLine(1, {0: anim.CharacterCell('1')}, 1000),
                session.DisplayLine(2, {0: cursor_char}, 1000),
                session.DisplayLine(0, {0: anim.CharacterCell('0')}, 0, 1042),
                session.DisplayLine(1, {0: anim.CharacterCell('1')}, 1000, 42),
                session.DisplayLine(2, {0: cursor_char}, 1000, 42),
            ]

            self.assertEqual(expected_events, events)

        # Test #2: Hidden cursor
        with self.subTest(case='Hidden cursor'):
            #   '\u001b[?25h' : display cursor
            #   '\u001b[?25l' : hide cursor
            records = [
                AsciiCastV2Header(version=2, width=80, height=24, theme=theme),
                AsciiCastV2Event(0, 'o', '\u001b[?25ha'.encode('utf-8'), 1),
                AsciiCastV2Event(1, 'o', '\r\n\u001b[?25lb'.encode('utf-8'), 1),
                AsciiCastV2Event(2, 'o', '\r\n\u001b[?25hc'.encode('utf-8'), 1),
            ]
            session = term.TerminalSession(records)
            events = list(session.line_events(1, None, 42))
            expected_events = [
                session.Configuration(80, 24),
                session.DisplayLine(0, {0: anim.CharacterCell('a'), 1: cursor_char}, 0),
                session.DisplayLine(0, {0: anim.CharacterCell('a'), 1: cursor_char}, 0, 1000),
                session.DisplayLine(0, {0: anim.CharacterCell('a')}, 1000),
                session.DisplayLine(1, {0: anim.CharacterCell('b')}, 1000),
                session.DisplayLine(2, {0: anim.CharacterCell('c'), 1: cursor_char}, 2000),
                session.DisplayLine(0, {0: anim.CharacterCell('a')}, 1000, 1042),
                session.DisplayLine(1, {0: anim.CharacterCell('b')}, 1000, 1042),
                session.DisplayLine(2, {0: anim.CharacterCell('c'), 1: cursor_char}, 2000, 42),
            ]
            self.assertEqual(expected_events, events)

    def test_get_terminal_size(self):
        with self.subTest(case='Successful get_terminal_size call'):
            term_size_mock = MagicMock(return_value=(42, 84))
            with patch('os.get_terminal_size', term_size_mock):
                cols, lines, = term.get_terminal_size(-1)
                self.assertEqual(cols, 42)
                self.assertEqual(lines, 84)

    def test__group_by_time(self):
        event_records = [
            AsciiCastV2Event(0, 'o', b'1', None),
            AsciiCastV2Event(5, 'o', b'2', None),
            AsciiCastV2Event(8, 'o', b'3', None),
            AsciiCastV2Event(20, 'o', b'4', None),
            AsciiCastV2Event(21, 'o', b'5', None),
            AsciiCastV2Event(30, 'o', b'6', None),
            AsciiCastV2Event(31, 'o', b'7', None),
            AsciiCastV2Event(32, 'o', b'8', None),
            AsciiCastV2Event(33, 'o', b'9', None),
            AsciiCastV2Event(43, 'o', b'10', None),
        ]

        with self.subTest(case='maximum record duration'):
            grouped_event_records_max = [
                AsciiCastV2Event(0, 'o', b'1', 5),
                AsciiCastV2Event(5, 'o', b'23', 6),
                AsciiCastV2Event(11, 'o', b'45', 6),
                AsciiCastV2Event(17, 'o', b'6789', 6),
                AsciiCastV2Event(23, 'o', b'10', 1.234),
            ]
            result = list(term._group_by_time(event_records, 5000, 6000, 1234))
            self.assertEqual(grouped_event_records_max, result)

        with self.subTest(case='no maximum record duration'):
            grouped_event_records_no_max = [
                AsciiCastV2Event(0, 'o', b'1', 5),
                AsciiCastV2Event(5, 'o', b'23', 15),
                AsciiCastV2Event(20, 'o', b'45', 10),
                AsciiCastV2Event(30, 'o', b'6789', 13),
                AsciiCastV2Event(43, 'o', b'10', 1.234),
            ]
            result = list(term._group_by_time(event_records, 5000, None, 1234))
            self.assertEqual(grouped_event_records_no_max, result)
