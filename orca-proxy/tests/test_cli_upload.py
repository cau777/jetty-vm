from orca_proxy import cli


def test_upload_reaches_daemon_with_file_mode(monkeypatch, tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'hello')
    calls = []
    def api(method, path, body):
        calls.append((method, path, body))
        return {'bytes_written': 5, 'path': '/tmp/dest'}
    monkeypatch.setattr(cli, '_api', api)
    assert cli.main(['vm', 'upload', 'agent', '/tmp/dest', '--file', str(source), '--mode', '600']) == 0
    assert calls == [('POST', '/api/v1/jetty/vms/agent/files', {'path': '/tmp/dest', 'content_b64': 'aGVsbG8=', 'mode': 0o600, 'owner': 'ubuntu'})]
