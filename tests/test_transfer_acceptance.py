from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

from sdilej_serialy import continuous, pipeline
from sdilej_serialy.models import Episode
from test_dual import source
from test_source_retry import stub_transfer
from test_target_resilience import run_transfer


def test_real_http_ack_is_checkpointed_before_completion(tmp_path, monkeypatch):
    stub_transfer(monkeypatch, lambda c, **kw: c)
    row = source()
    episode = Episode.from_dict(row['episode'])
    snapshots = []
    state = pipeline.EpisodeState(tmp_path / 'state.json', on_save=lambda p: snapshots.append(p.read_text()))
    received, waiting = Event(), Event()
    violations, errors, threads = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            received.set()
            if not waiting.wait(3) or state.uploaded(episode):
                violations.append('Completed before HTTP acceptance')
            self.send_response(201)
            self.send_header('Content-Length', '0')
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = Thread(target=server.serve_forever)
    server_thread.start()

    class Body(bytes):
        pass

    def relay(*args, on_prepared, upload_requester, **kwargs):
        on_prepared('555', 100)
        finished = upload_requester.finished
        def wait():
            waiting.set()
            assert finished.wait(5)
        upload_requester.finished = SimpleNamespace(wait=wait, is_set=finished.is_set)
        body = Body(b'x' * 100)
        body.fields = [('files', ('file.mkv', SimpleNamespace(position=100, total=100), 'video/mp4'))]
        def post():
            try:
                upload_requester(f'http://127.0.0.1:{server.server_port}/', data=body, timeout=5)
            except Exception as error:
                errors.append(type(error).__name__)
        thread = Thread(target=post)
        threads.append(thread)
        thread.start()
        assert received.wait(3)
        return SimpleNamespace(video_id='555')

    monkeypatch.setattr(continuous.prehrajto, 'relay_upload', relay)
    try:
        run_transfer([row], state, Event(), Event())
        assert state.uploaded(episode)
        assert not violations and not errors
        receipt_index = next(i for i, s in enumerate(snapshots) if 'transfer_receipt' in s)
        upload_index = next(i for i, s in enumerate(snapshots) if '"upload"' in s)
        assert receipt_index < upload_index
    finally:
        for thread in threads:
            thread.join(6)
        server.shutdown()
        server_thread.join()
        server.server_close()
