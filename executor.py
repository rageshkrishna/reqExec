"""
Executor executes a script, parses consoles and posts console logs
"""
import json
import subprocess
import threading
import time
import uuid
import os
from config import Config
from shippable_adapter import ShippableAdapter

class Executor(object):
    """
    Sets up config for the job, defaults for exit code and consoles
    """
    def __init__(self, script, job_envs_path):
        #
        # Private
        #
        self._script = script
        self._config = Config(job_envs_path)

        self._shippable_adapter = ShippableAdapter(
            self._config['SHIPPABLE_API_URL'],
            self._config['BUILDER_API_TOKEN']
        )

        self._is_executing = False

        # Consoles
        self._console_buffer = []
        self._console_buffer_lock = threading.Lock()
        # Console state
        self._current_group_info = None
        self._current_group_name = None
        self._current_cmd_info = None
        self._show_group = None

        #
        # Public
        #
        self.exit_code = 0

    def execute(self):
        """
        Starts threads to execute the script and flush consoles
        """
        script_runner_thread = threading.Thread(target=self._script_runner)
        script_runner_thread.start()

        # Wait for the execution to complete.
        self._is_executing = True
        console_flush_timer = threading.Timer(
            self._config['CONSOLE_FLUSH_INTERVAL_SECONDS'],
            self._set_console_flush_timer
        )
        console_flush_timer.start()
        script_runner_thread.join()
        self._is_executing = False
        self._flush_console_buffer()

    def _script_runner(self):
        """
        Runs the script, handles console output and finally sets the exit code
        """
        # We need to unset the LD_LIBRARY_PATH set by pyinstaller. This
        # will ensure the script prefers libraries on system rather
        # than the ones bundled during build time.
        env = dict(os.environ)
        env.pop('LD_LIBRARY_PATH', None)

        proc = subprocess.Popen(
            self._script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self._config['BUILD_DIR'],
            env=env
        )

        for line in iter(proc.stdout.readline, ''):
            is_script_success, is_complete = self._handle_console_line(line)

            if not is_script_success:
                self.exit_code = 1

            if is_complete:
                break

        proc.kill()

    def _handle_console_line(self, line):
        """
        Parses a single line of console output and pushes it to buffer
        This also returns whether the console line is successful and the
        script is complete
        """
        is_script_success = True
        is_complete = False
        timestamp = Executor._get_timestamp()
        line_split = line.split('|')
        if line.startswith('__SH__GROUP__START__'):
            self._current_group_info = line_split[1]
            self._current_group_name = '|'.join(line_split[2:])
            self._current_group_info = json.loads(self._current_group_info)
            self._show_group = self._current_group_info.get('is_shown', True)
            if self._show_group == 'false':
                self._show_group = False
            console_out = {
                'consoleId': self._current_group_info.get('id'),
                'parentConsoleId': 'root',
                'type': 'grp',
                'message': self._current_group_name,
                'timestamp': timestamp,
                'isShown': self._show_group
            }
            self._append_to_console_buffer(console_out)
        elif line.startswith('__SH__CMD__START__'):
            self._current_cmd_info = line_split[1]
            current_cmd_name = '|'.join(line_split[2:])
            self._current_cmd_info = json.loads(self._current_cmd_info)
            parent_id = self._current_group_info.get('id') if \
                self._current_group_info else None
            console_out = {
                'consoleId': self._current_cmd_info.get('id'),
                'parentConsoleId': parent_id,
                'type': 'cmd',
                'message': current_cmd_name,
                'timestamp': timestamp,
            }
            if parent_id:
                self._append_to_console_buffer(console_out)
        elif line.startswith('__SH__CMD__END__'):
            current_cmd_end_info = line_split[1]
            current_cmd_end_name = '|'.join(line_split[2:])
            current_cmd_end_info = json.loads(current_cmd_end_info)
            parent_id = self._current_group_info.get('id') if \
                self._current_group_info else None
            is_cmd_success = False
            if current_cmd_end_info.get('exitcode') == '0':
                is_cmd_success = True
            console_out = {
                'consoleId': self._current_cmd_info.get('id'),
                'parentConsoleId': parent_id,
                'type': 'cmd',
                'message': current_cmd_end_name,
                'timestamp': timestamp,
                'timestampEndedAt': timestamp,
                'isSuccess': is_cmd_success,
                'isShown': self._show_group
            }
            if parent_id:
                self._append_to_console_buffer(console_out)
        elif line.startswith('__SH__GROUP__END__'):
            current_grp_end_info = line_split[1]
            current_grp_end_name = '|'.join(line_split[2:])
            current_grp_end_info = json.loads(current_grp_end_info)
            is_cmd_success = False
            if current_grp_end_info.get('exitcode') == '0':
                is_cmd_success = True
            console_out = {
                'consoleId': self._current_group_info.get('id'),
                'parentConsoleId': 'root',
                'type': 'grp',
                'message': current_grp_end_name,
                'timestamp': timestamp,
                'timestampEndedAt': timestamp,
                'isSuccess': is_cmd_success,
                'isShown': self._show_group
            }
            self._append_to_console_buffer(console_out)
        elif line.startswith('__SH__SCRIPT_END_SUCCESS__'):
            is_script_success = True
            is_complete = True
        elif line.startswith('__SH__SCRIPT_END_FAILURE__'):
            is_script_success = False
            is_complete = True
        else:
            parent_id = self._current_cmd_info.get('id') if \
                self._current_cmd_info else None
            console_out = {
                'consoleId': str(uuid.uuid4()),
                'parentConsoleId': parent_id,
                'type': 'msg',
                'message': line,
                'timestamp': timestamp,
            }
            if parent_id:
                self._append_to_console_buffer(console_out)

        return is_script_success, is_complete

    def _append_to_console_buffer(self, console_out):
        """
        Pushes a console line to buffer after taking over lock
        """
        with self._console_buffer_lock:
            self._console_buffer.append(console_out)

        if len(self._console_buffer) > self._config['CONSOLE_BUFFER_LENGTH']:
            self._flush_console_buffer()

    def _set_console_flush_timer(self):
        """
        Calls _flush_console_buffer to flush console buffers in constant
        intervals and stops when the script is finished execution
        """
        if not self._is_executing:
            return

        self._flush_console_buffer()
        console_flush_timer = threading.Timer(
            self._config['CONSOLE_FLUSH_INTERVAL_SECONDS'],
            self._set_console_flush_timer
        )
        console_flush_timer.start()

    def _flush_console_buffer(self):
        """
        Flushes console buffers after taking over lock
        """
        if self._console_buffer:
            with self._console_buffer_lock:
                req_body = {
                    'buildJobId': self._config['BUILD_JOB_ID'],
                    'buildJobConsoles': self._console_buffer
                }

            self._shippable_adapter.post_build_job_consoles(req_body)

            del self._console_buffer
            self._console_buffer = []

    @staticmethod
    def _get_timestamp():
        """
        Helper method to return timestamp in a format which is acceptable
        for consoles
        """
        return int(time.time() * 1000000)
