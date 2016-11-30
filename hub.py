#!/usr/bin/env python3

import collections
import contextlib
import datetime
import functools
import itertools
import json
import logging
import os
import gc
import random
import re
import resource
import signal
import time

import leveldb
import redis
import tornado.ioloop
import tornado.options
import tornado.gen
import tornado.web

import tasks_publisher
import public_suffix
import sqliteset
import cz88_ip


is_valid_host = re.compile(
    r"([-a-z0-9]{1,64}\.)+"
    r"[a-z]{2,16}"
    r"(:[0-9]{2,5})?"
).fullmatch


class BaseHandler(tornado.web.RequestHandler):
    tasks = tasks_publisher.Tasks("hosts/queue")
    db = leveldb.LevelDB("hosts.ldb")
    redis_cli = redis.StrictRedis(unix_socket_path="etc/.redis.sock",
                                  decode_responses=True)

    def set_default_headers(self):
        self.set_header("Content-Type", "text/plain; charset=UTF-8")

    def write_json(self, obj):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.finish(json.dumps(obj, default=str, ensure_ascii=False, indent=4,
                               sort_keys=True, separators=(",", ": ")))


class MainHandler(BaseHandler):
    def get(self):
        gc.collect()


class CommandHandler(BaseHandler):
    def post(self):
        cmd = self.request.body.decode()
        if cmd == "renew":
            self.tasks.text.renew()
            self.redis_cli.delete("suffixes_warned")
        else:
            raise tornado.web.HTTPError(404)


class HostHandler(BaseHandler):
    workers = collections.defaultdict(dict)

    def get(self):
        resp = {}
        host = self.tasks.get()
        if host is None:
            raise tornado.web.HTTPError(404)
        resp["host"] = host
        self.write_json(resp)

    def post(self):
        host = self.request.body.decode()
        self.tasks.add(host)


from simple_scan import page1 as _page1, domain_pattern as _domain_pattern
def _simple_check(host_name, info):
    if _domain_pattern.search(host_name):
        return True
    if sum(_page1(info)):
        return True
    return False


class HostInfoHandler(BaseHandler):
    ignored_hosts_set = sqliteset.Set(0x100, "hosts/ignored.set")
    with open("hosts/ignored_suffixes") as f:
        ignored_suffixes = set(i for i in f.read().split() if i)
        ignored_suffixes.add(None)  # ignore unknown host suffix
    del f

    def get(self, name):
        try:
            self.write(bytes(self.db.Get(name.encode())))
        except KeyError:
            raise tornado.web.HTTPError(404)

    def post(self, name):
        ts = time.strftime("%Y%m%d-%H%M")
        hincrby = self.redis_cli.hincrby
        hincrby("cnt", "done")
        hincrby("cnt_done", ts)
        hincrby("cnt_done", ts[:-2])
        content = self.request.body
        info = json.loads(content.decode())
        self._notice(name, info)

        other_hosts_found = info.get("other_hosts_found")
        if other_hosts_found:
            #self._(other_hosts_found)
            valued, ignored = [], []
            suffixes = collections.Counter()
            for i in other_hosts_found:
                _ = public_suffix.split(i)
                if not _:
                    continue
                suffix, *levels = _
                if suffix not in self.ignored_suffixes:
                    suffixes[suffix] += 1
                    if suffixes[suffix] > 99:  # this batch of other_hosts_found
                        self.ignored_suffixes.add(suffix)
                    if len(levels) > 2:  # ignore ...3.2.1.com
                        ignored.append(i)
                    elif len(levels) == 2 and len(levels[1]) >= 5:  # ignore 1fkfw.xxx.net
                        ignored.append(i)
                    else:
                        valued.append(i)
                else:
                    ignored.append(i)

            n_found = self.tasks.add(*valued)
            n_found += self.ignored_hosts_set.add(*ignored)
            if n_found:
                hincrby("cnt", "found")
                hincrby("cnt_found", ts, n_found)
                hincrby("cnt_found", ts[:-2], n_found)

            for k, v in suffixes.items():
                if v > 2:
                    n = hincrby("suffixes_warned", k, v)
                    if n > 99:  # accumulated
                        self.ignored_suffixes.add(k)
                        with open("hosts/ignored_suffixes", "a") as f:
                            print(k, file=f)
                        self.redis_cli.hdel("suffixes_warned", k)

        redirect = info.get("redirect")
        if redirect and is_valid_host(redirect):
            self.tasks.add(redirect)

        self.db.Put(name.encode(), content)

    def delete(self, name):
        self.db.Delete(name.encode())

    def _notice(self, name, info):
        log = {
            "host": name,
            "bad": _simple_check(name, info),
        }

        if log["bad"]:
            self.redis_cli.hincrby("cnt", "bad", 1)

        try:
            log["location"] = cz88_ip.find(info["ip"])
        except Exception:
            "logging.exception(info)"

        TailHandler.pub(log)


class MailHandler(BaseHandler):
    workers = {}
    commands = {}
    callbacks = {}

    @classmethod
    def launch_command(cls, id, cmd):
        cls.commands.setdefault(id, {}).update(cmd)
        f = cls.callbacks.pop(id, None)
        if f:
            f()

    @tornado.web.asynchronous
    def get(self, id):
        def f():
            self.write_json(self.commands.pop(id))
        if id in self.commands:
            f()
        else:
            self.callbacks[id] = tornado.stack_context.wrap(f)

    def post(self, id):
        info = json.loads(self.request.body.decode())
        info["active"] = datetime.datetime.now()
        self.workers[id] = info


class WorkerHandler(MailHandler):
    @tornado.gen.coroutine
    def get(self, id=None):
        yield from self._update_workers([id] if id else list(self.workers))
        info = self.workers
        if id:
            info = info[id]
        self.write_json(info)

    def _update_workers(self, lst):
        ts = datetime.datetime.now()
        delta = datetime.timedelta(seconds=10)
        for id in lst:
            self.launch_command(id, {})
        while True:
            yield tornado.gen.sleep(0.1)
            if all(self.workers[id]["active"] > ts for id in lst):
                break
            now = datetime.datetime.now()
            dead = [id for id in lst if now - self.workers[id]["active"] > delta]
            for id in dead:
                lst.remove(id)
                self.workers.pop(id)

    def post(self, id):
        self.launch_command(id, json.loads(self.request.body.decode()))


class StatusHandler(BaseHandler):
    def get(self, name):
        return getattr(self, "get_status_" + name)()

    def get_status_cnt(self):
        cnt = {k: int(v) for k, v in self.redis_cli.hgetall("cnt").items()}
        analysed = cnt["done"] - random.randint(10000, 20000)
        if cnt.get("analysed", 0) < analysed:
            self.redis_cli.hset("cnt", "analysed", analysed)
        self.write_json(cnt)

    def get_status_recent(self):
        recent = {}
        dt = datetime.datetime.now()
        p = self.redis_cli.pipeline()

        l = [(dt - datetime.timedelta(minutes=i)).strftime("%Y%m%d-%H%M") for i in reversed(range(60))]
        for i in l:
            p.hincrby('cnt_done', i, 0)
        recent["minutes"] = p.execute()

        l = [(dt - datetime.timedelta(hours=i)).strftime("%Y%m%d-%H") for i in reversed(range(48))]
        for i in l:
            p.hincrby('cnt_done', i, 0)
        recent["hours"] = p.execute()

        self.write_json(recent)


class TailHandler(BaseHandler):
    _todos = collections.defaultdict(list)
    _callbacks = []

    @classmethod
    def pub(cls, log):
        discards = []
        for token, todo in cls._todos.items():
            todo.append(log)
            if len(todo) >= 300:
                discards.append(token)
        for token in discards:
            cls._todos.pop(token)
        for f in cls._callbacks:
            f()
        cls._callbacks.clear()

    @tornado.web.asynchronous
    def get(self, token):
        todo = self._todos[token]
        def f():
            self.set_header("Cache-Control", "no-cache")
            self.write_json(todo)
            todo.clear()
        if todo:
            f()
        else:
            self._callbacks.append(tornado.stack_context.wrap(f))
            # ... and this request is not finished


handlers = [
    (r"/host", HostHandler),
    (r"/host-info/(.+)", HostInfoHandler),
    (r"/tail/(.+)", TailHandler),
    (r"/status/(.+)", StatusHandler),
    (r"/mail/(.+)", MailHandler),
    (r"/workers", WorkerHandler),
    (r"/worker/(.+)", WorkerHandler),
    (r"/_cmd", CommandHandler),
    (r"/(.+)", tornado.web.StaticFileHandler, {"path": "html"}),
    (r"/", MainHandler),
]


def main():
    #gc.disable()
    _, n = resource.getrlimit(resource.RLIMIT_NOFILE)
    if n < 2000:
        raise Warning("RLIMIT_NOFILE", n)
    resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))

    tornado.options.parse_command_line()

    p = int(os.environ.get("PORT", 1033))
    tornado.web.Application(
        handlers,
    ).listen(p, xheaders=True)

    with contextlib.suppress(ImportError):
        import tornadospy
        tornadospy.listen(p + 1)

    io_loop = tornado.ioloop.IOLoop.instance()

    def _term(*_):
        #io_loop.close(True)
        io_loop.stop()
        BaseHandler.tasks.close()
        logging.info("stop")

    signal.signal(signal.SIGTERM, _term)

    logging.info("start")
    io_loop.start()


if __name__ == "__main__":
    main()
    """
    curl -d "renew" 'localhost:1033/_cmd'
    curl -d "rebuild" 'localhost:1033/_cmd'
    """
