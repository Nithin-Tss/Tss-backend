"""
Keeps each test run's SQLite worker copies in a folder of their own.

On macOS, parallel tests start workers with "spawn", and Django copies the in-memory
test database to "default_<n>.sqlite3" in the current folder. Two runs at the same time
then share those files, and the first to finish deletes them under the other
(FileNotFoundError: 'default_1.sqlite3' on teardown). The folder name goes into an
environment variable, so spawned workers, which inherit it, open the same files.
"""
import os
import tempfile

from django.db.backends.sqlite3.creation import DatabaseCreation as BaseCreation

_ENV = "TSS_TEST_DB_DIR"


def _run_dir():
    path = os.environ.get(_ENV)
    if not path:
        path = os.environ[_ENV] = tempfile.mkdtemp(prefix="tss-test-db-")
    return path


class DatabaseCreation(BaseCreation):
    def get_test_db_clone_settings(self, suffix):
        clone = super().get_test_db_clone_settings(suffix)
        name = clone["NAME"]
        if name != self.connection.settings_dict["NAME"] and not os.path.isabs(name):
            clone = {**clone, "NAME": os.path.join(_run_dir(), name)}
        return clone
