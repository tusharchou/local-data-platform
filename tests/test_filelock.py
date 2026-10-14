"""The file lock that a table and the scheduler share: one holder at a time, a lock that goes with its process."""

import subprocess
import sys
import threading

from local_data_platform.filelock import file_lock, lock_file, unlock_file

HOLDER = """
import sys
from local_data_platform.filelock import file_lock

with file_lock(sys.argv[1]):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_a_free_lock_is_held_inside_the_block_and_free_again_after_it(tmp_path):
    path = tmp_path / "a.lock"

    with file_lock(path, blocking=False) as held:
        assert held is True

    with file_lock(path, blocking=False) as again:
        assert again is True


def test_a_second_handle_cannot_take_a_lock_that_is_held_even_in_the_same_process(tmp_path):
    path = tmp_path / "a.lock"

    with file_lock(path):
        with file_lock(path, blocking=False) as held:
            assert held is False
        with open(path, "a+b") as other:
            assert lock_file(other, blocking=False) is False

    with open(path, "a+b") as other:
        assert lock_file(other, blocking=False) is True
        unlock_file(other)


def test_a_lock_that_is_given_up_with_unlock_file_is_free_while_the_file_is_still_open(tmp_path):
    path = tmp_path / "a.lock"

    with open(path, "a+b") as first, open(path, "a+b") as second:
        assert lock_file(first, blocking=False) is True
        assert lock_file(second, blocking=False) is False
        unlock_file(first)
        assert lock_file(second, blocking=False) is True, "free, though the first handle is still open"


def test_a_block_that_did_not_get_the_lock_does_not_free_the_lock_of_the_holder(tmp_path):
    path = tmp_path / "a.lock"

    with file_lock(path):
        with file_lock(path, blocking=False):
            pass
        with file_lock(path, blocking=False) as held:
            assert held is False, "the holder still holds it"


def test_the_lock_file_and_its_folders_are_made_and_stay(tmp_path):
    path = tmp_path / "project" / ".ldp" / "locks" / "schedule.lock"

    with file_lock(path):
        assert path.is_file()

    assert path.is_file()


def test_a_blocking_lock_waits_for_the_holder_to_let_go(tmp_path):
    path = tmp_path / "a.lock"
    got_it = threading.Event()

    def wait_for_it():
        with file_lock(path):
            got_it.set()

    with file_lock(path):
        waiter = threading.Thread(target=wait_for_it)
        waiter.start()
        assert not got_it.wait(0.3), "it waits while the lock is held"
    assert got_it.wait(30), "and gets the lock once it is free"
    waiter.join(30)


def test_a_lock_is_given_up_when_the_block_raises(tmp_path):
    path = tmp_path / "a.lock"

    try:
        with file_lock(path):
            raise RuntimeError("the work failed")
    except RuntimeError:
        pass

    with file_lock(path, blocking=False) as held:
        assert held is True


def test_another_process_that_holds_the_lock_keeps_this_one_out_until_it_dies(tmp_path):
    path = tmp_path / "a.lock"
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with file_lock(path, blocking=False) as held:
            assert held is False
        child.kill()  # a process that dies leaves no lock behind
        child.wait(30)
        with file_lock(path, blocking=False) as held:
            assert held is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(30)
        child.stdin.close()
        child.stdout.close()
