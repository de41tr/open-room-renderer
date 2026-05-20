from fake_linkplay.http_api import parse_command


def test_play_url_with_colons_slashes_query() -> None:
    parsed = parse_command("setPlayerCmd:play:https%3A%2F%2Fexample.test%2Fa%3Fb%3Dc%3Ad")
    assert parsed.name == "play"
    assert parsed.args == ("https://example.test/a?b=c:d",)


def test_playlist_command_parsing() -> None:
    parsed = parse_command("setPlayerCmd:playlist:https%3A%2F%2Fexample.test%2Flist.m3u:3")
    assert parsed.name == "playlist"
    assert parsed.args == ("https://example.test/list.m3u", "3")


def test_pause_resume_stop_parsing() -> None:
    assert parse_command("setPlayerCmd:pause").name == "pause"
    assert parse_command("setPlayerCmd:resume").name == "resume"
    assert parse_command("setPlayerCmd:stop").name == "stop"


def test_volume_bounds_command_name() -> None:
    parsed = parse_command("setPlayerCmd:vol:101")
    assert parsed.name == "volume"
    assert parsed.args == ("101",)
