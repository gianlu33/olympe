# -*- coding: UTF-8 -*-

#  Copyright (C) 2019 Parrot Drones SAS
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions
#  are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in
#    the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the Parrot Company nor the names
#    of its contributors may be used to endorse or promote products
#    derived from this software without specific prior written
#    permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
#  FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
#  PARROT COMPANY BE LIABLE FOR ANY DIRECT, INDIRECT,
#  INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
#  BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS
#  OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
#  AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
#  OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
#  SUCH DAMAGE.


from __future__ import absolute_import
from __future__ import unicode_literals
from aenum import IntFlag

import concurrent.futures
import ctypes
import logging
import olympe_deps as od
import os
import threading


try:
    from itertools import ifilter as filter
except ImportError:
    # python3
    pass


logger = logging.getLogger("concurrent.futures")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.DEBUG)


class PompEvent(IntFlag):
    IN = od.POMP_FD_EVENT_IN
    PRI = od.POMP_FD_EVENT_PRI
    OUT = od.POMP_FD_EVENT_OUT
    ERR = od.POMP_FD_EVENT_ERR
    HUP = od.POMP_FD_EVENT_HUP


class Future(concurrent.futures.Future):

    """
    A chainable Future class
    """

    def __init__(self, loop):
        super(Future, self).__init__()
        self._loop = loop

    def _register(self):
        self._loop._register_future(id(self))
        self.add_done_callback(lambda _: self._loop._unregister_future(
            id(self), ignore_error=True))

    def __del__(self):
        self._loop._unregister_future(id(self), ignore_error=True)

    def set_from(self, source):
        if source.cancelled():
            self.cancel()
            return
        if self.done():
            return
        if not self.set_running_or_notify_cancel():
            return
        exception = source.exception()
        if exception is not None:
            self.set_exception(exception)
        else:
            result = source.result()
            self.set_result(result)

    def chain(self, next_):
        self.add_done_callback(lambda _: next_.set_from(self))

    def then(self, fn, deferred=False):
        result = Future(self._loop)
        result._register()

        def callback(_):
            try:
                if deferred:
                    temp = self._loop.run_later(fn, self.result())
                    temp.chain(result)
                elif not threading.current_thread() is self._loop:
                    temp = self._loop.run_async(fn, self.result())
                    temp.chain(result)
                else:
                    result.set_result(fn(self.result()))
            except Exception as e:
                self.logging.exception(
                    "Unhandled exception while chaining futures"
                )
                result.set_exception(e)
            except:
                result.cancel()

        self.add_done_callback(callback)
        return result

    def result_or_cancel(self, timeout=None):
        try:
            return self.result(timeout=timeout)
        except:
            self.cancel()
            raise


class PompLoopThread(threading.Thread):
    """
    Class running a pomp loop in a pomp thread.
    It performs all calls to pomp and arsdk-ng within the loop (except init and destruction)
    """

    def __init__(self, logging):
        self.logging = logging

        self.running = False
        self.pomptimeout_ms = 100
        self.async_pomp_task = list()
        self.deferred_pomp_task = list()
        self.wakeup_evt = od.pomp_evt_new()
        self.pomp_events = dict()
        self.pomp_event_callbacks = dict()
        self.pomp_loop = None
        self.pomp_timers = {}
        self.pomp_timer_callbacks = {}
        self.evt_userdata = dict()
        self.fd_userdata = dict()
        self.c_fd_userdata = dict()
        self.c_evt_userdata = dict()
        self.pomp_fd_callbacks = dict()
        self.cleanup_functions = []
        self.futures = []

        self._create_pomp_loop()

        super(PompLoopThread, self).__init__()

    def destroy(self):
        # stop the thread
        self.stop()
        self._remove_event_from_loop(self.wakeup_evt)

        # remove all fds from the loop
        self._destroy_pomp_loop_fds()

        # remove all timers from the loop
        self._destroy_pomp_loop_timers()

        # destroy the loop
        self._destroy_pomp_loop()

    def start(self):
        self.running = True
        super().start()

    def stop(self):
        """
        Stop thread to manage commands send to the drone
        """
        if not self.running:
            return False
        self.running = False
        if threading.current_thread().ident != self.ident:
            self._wake_up()
            self.join()
        return True

    def run_async(self, func, *args, **kwds):
        """
        Fills in a list with the function to be executed in the pomp thread
        and wakes up the pomp thread.
        """
        future = Future(self)
        future._register()

        if threading.current_thread() is not self:
            self.async_pomp_task.append((future, func, args, kwds))
            self._wake_up()
        else:
            try:
                ret = func(*args, **kwds)
            except Exception as e:
                self.logging.exception(
                    "Unhandled exception in async task function"
                )
                future.set_exception(e)
            else:
                if not isinstance(ret, concurrent.futures.Future):
                    future.set_result(ret)
                else:
                    ret.chain(future)
        return future

    def run_later(self, func, *args, **kwds):
        """
        Fills in a list with the function to be executed later in the pomp thread
        """
        future = Future(self)
        future._register()
        self.deferred_pomp_task.append((future, func, args, kwds))
        return future

    def _wake_up_event_cb(self, pomp_evt, _userdata):
        """
        Callback received when a pomp_evt is triggered.
        """
        # the pomp_evt is acknowledged by libpomp

    def _run_task_list(self, task_list):
        """
        execute all pending functions located in the task list
        this is done in the order the list has been filled in
        """
        while len(task_list):
            future, f, args, kwds = task_list.pop(0)
            try:
                ret = f(*args, **kwds)
            except Exception as e:
                self.logging.exception(
                    "Unhandled exception in async task function"
                )
                self._unregister_future(future, ignore_error=True)
                future.set_exception(e)
                continue
            if not isinstance(ret, concurrent.futures.Future):
                future.set_result(ret)
            else:
                ret.chain(future)

    def run(self):
        """
        Thread's main loop
        """
        self._add_event_to_loop(
            self.wakeup_evt, lambda *args: self._wake_up_event_cb(*args)
        )

        # We have to monitor the main thread exit. This is the simplest way to
        # let the main thread handle the signals while still being able to
        # perform some cleanup before the process exit. If we don't monitor the
        # main thread, this thread will hang the process when the process
        # receive SIGINT (or any other non fatal signal).
        main_thread = next(
            filter(lambda t: t.name == "MainThread", threading.enumerate())
        )
        try:
            while self.running and main_thread.is_alive():
                try:
                    self._wait_and_process()
                except RuntimeError as e:
                    self.logging.error("Exception caught: {}".format(e))

                self._run_task_list(self.async_pomp_task)
                self._run_task_list(self.deferred_pomp_task)
        finally:
            self.running = False
            # Perform some cleanup before this thread dies
            self._cleanup()
            self.destroy()

    def _wait_and_process(self):
        od.pomp_loop_wait_and_process(self.pomp_loop, self.pomptimeout_ms)

    def _wake_up(self):
        if self.wakeup_evt:
            od.pomp_evt_signal(self.wakeup_evt)

    def add_fd_to_loop(self, fd, cb, fd_events, userdata=None):
        return self.run_async(self._add_fd_to_loop, fd, cb, fd_events, userdata=userdata)

    def has_fd(self, fd):
        try:
            return self.run_async(self._has_fd, fd).result_or_cancel(timeout=5)
        except concurrent.futures.TimeoutError:
            return False

    def _has_fd(self, fd):
        return bool(od.pomp_loop_has_fd(self.pomp_loop, fd) == 1)

    def _add_fd_to_loop(self, fd, cb, fd_events, userdata=None):
        if cb is None:
            self.logging.info(
                "Cannot add fd '{}' to pomp loop without "
                "a valid callback function".format(fd)
            )
            return None
        self.fd_userdata[fd] = userdata
        userdata = ctypes.cast(
            ctypes.pointer(ctypes.py_object(userdata)), ctypes.c_void_p
        )
        self.c_fd_userdata[fd] = userdata
        self.pomp_fd_callbacks[fd] = od.pomp_fd_event_cb_t(cb)
        res = od.pomp_loop_add(
            self.pomp_loop,
            ctypes.c_int32(fd),
            od.uint32_t(int(fd_events)),
            self.pomp_fd_callbacks[fd],
            userdata
        )
        if res != 0:
            raise RuntimeError(
                "Cannot add fd '{}' to pomp loop: {} ({})".format(
                    fd, os.strerror(-res), res)
            )

    def remove_fd_from_loop(self, fd):
        return self.run_async(self._remove_fd_from_loop, fd)

    def _remove_fd_from_loop(self, fd):
        self.fd_userdata.pop(fd, None)
        self.c_fd_userdata.pop(fd, None)
        if self.pomp_fd_callbacks.pop(fd, None) is not None:
            if od.pomp_loop_remove(self.pomp_loop, fd) != 0:
                self.logging.error("Cannot remove fd '{}' from pomp loop".format(fd))
                return False
        return True

    def add_event_to_loop(self, *args, **kwds):
        """
        Add a pomp event to the loop
        """
        self.run_async(self._add_event_to_loop, *args, **kwds)

    def _add_event_to_loop(self, pomp_evt, cb, userdata=None):
        evt_id = id(pomp_evt)
        self.pomp_events[evt_id] = pomp_evt
        self.pomp_event_callbacks[evt_id] = od.pomp_evt_cb_t(cb)

        self.evt_userdata[evt_id] = userdata
        userdata = ctypes.cast(
            ctypes.pointer(ctypes.py_object(userdata)), ctypes.c_void_p
        )
        self.c_evt_userdata[evt_id] = userdata
        res = od.pomp_evt_attach_to_loop(
            pomp_evt, self.pomp_loop, self.pomp_event_callbacks[evt_id], userdata
        )
        if res != 0:
            raise RuntimeError("Cannot add eventfd to pomp loop")

    def remove_event_from_loop(self, *args, **kwds):
        """
        Remove a pomp event from the loop
        """
        self.run_later(self._remove_event_from_loop, *args, **kwds)

    def _remove_event_from_loop(self, pomp_evt):
        evt_id = id(pomp_evt)
        self.evt_userdata.pop(evt_id, None)
        self.c_evt_userdata.pop(evt_id, None)
        self.pomp_event_callbacks.pop(evt_id, None)
        if self.pomp_events.pop(evt_id, None) is not None:
            if od.pomp_evt_detach_from_loop(pomp_evt, self.pomp_loop) != 0:
                self.logging.error('Cannot remove event "%s" from pomp loop' % evt_id)

    def _destroy_pomp_loop_fds(self):
        evts = list(self.pomp_events.values())[:]
        for evt in evts:
            self._remove_event_from_loop(evt)
        fds = list(self.pomp_fd_callbacks.keys())[:]
        for fd in fds:
            self._remove_fd_from_loop(fd)

    def _create_pomp_loop(self):

        self.logging.info("Creating pomp loop")
        self.pomp_loop = od.pomp_loop_new()

        if self.pomp_loop is None:
            raise RuntimeError("Cannot create pomp loop")

    def _destroy_pomp_loop(self):
        if self.pomp_loop is not None:
            res = od.pomp_loop_destroy(self.pomp_loop)

            if res != 0:
                self.logging.error(
                    "Error while destroying pomp loop: {}".format(res))
                return False
            else:
                self.logging.info("Pomp loop has been destroyed")
        self.pomp_loop = None
        return True

    def create_timer(self, callback):

        self.logging.info("Creating pomp timer")

        pomp_callback = od.pomp_timer_cb_t(lambda *args: callback(*args))

        pomp_timer = od.pomp_timer_new(self.pomp_loop, pomp_callback, None)

        if pomp_timer is None:
            raise RuntimeError("Unable to create pomp timer")

        self.pomp_timers[id(pomp_timer)] = pomp_timer
        self.pomp_timer_callbacks[id(pomp_timer)] = pomp_callback
        return pomp_timer

    def set_timer(self, pomp_timer, delay, period):
        res = od.pomp_timer_set_periodic(pomp_timer, delay, period)

        return res == 0

    def clear_timer(self, pomp_timer):
        res = od.pomp_timer_clear(pomp_timer)

        return res == 0

    def destroy_timer(self, pomp_timer):
        if id(pomp_timer) not in self.pomp_timers:
            return False

        res = od.pomp_timer_destroy(pomp_timer)

        if res != 0:
            self.logging.error(
                "Error while destroying pomp loop timer: {}".format(res))
            return False
        else:
            del self.pomp_timers[id(pomp_timer)]
            del self.pomp_timer_callbacks[id(pomp_timer)]
            self.logging.info("Pomp loop timer has been destroyed")

        return True

    def _destroy_pomp_loop_timers(self):
        pomp_timers = list(self.pomp_timers.values())[:]
        for pomp_timer in pomp_timers:
            self.destroy_timer(pomp_timer)

    def register_cleanup(self, fn):
        self.cleanup_functions.append(fn)

    def unregister_cleanup(self, fn, ignore_error=False):
        try:
            self.cleanup_functions.remove(fn)
        except ValueError:
            if not ignore_error:
                raise

    def _cleanup(self):
        # Execute cleanup functions
        for cleanup in reversed(self.cleanup_functions):
            try:
                cleanup()
            except Exception:
                self.logging.exception(
                    "Unhandled exception in cleanup function"
                )
        self.cleanup_functions = []

        # Execute asynchronous cleanup actions
        count = 0
        while self.async_pomp_task or self.deferred_pomp_task or self.futures:
            self._wait_and_process()
            self._run_task_list(self.async_pomp_task)
            self._run_task_list(self.deferred_pomp_task)
            if count > 30:
                self.logging.error('Deferred cleanup action are still pending after 3s')
                break
            count += 1

        self.async_pomp_task = []
        self.deferred_pomp_task = []
        self.futures = []

    def _register_future(self, f):
        self.futures.append(f)

    def _unregister_future(self, f, ignore_error=False):
        try:
            self.futures.remove(f)
        except ValueError:
            if not ignore_error:
                raise
