"""Pure authored-playlist transformations preserve URL occurrence identity."""
from __future__ import annotations

from copy import deepcopy

import pytest

from fake_linkplay.playlist_edits import append_playlist, move_playlist, remove_playlist
from fake_linkplay.playlist_files import MAX_PLAYLIST_URLS, PLAYLIST_MODES, PlaylistError
from fake_linkplay.queue_client import QueueClientError

NAME = "Edited café 夜 🎵"
URLS = ["https://media.example.test/音声/a%2fb?token=synthetic-secret&a=1+2#片",
        "http://[::1]:8080/track%2FB.wav?x=a%2520b&x=&case=%2f%2F"]


def payload(**changes):
    return {"version": 1, "name": NAME, "index": 3, "mode": "repeat-all",
            "urls": [URLS[0], URLS[1], URLS[0]], **changes}


@pytest.mark.parametrize("mode", PLAYLIST_MODES)
def test_all_edits_preserve_metadata_exact_urls_duplicates_and_source(mode):
    source = payload(mode=mode)
    original = deepcopy(source)
    appended = append_playlist(source, [URLS[1], URLS[0]])
    removed = remove_playlist(source, 1)
    moved = move_playlist(source, 3, 1)
    for result, urls, index in (
        (appended, [*source["urls"], URLS[1], URLS[0]], 3),
        (removed, [URLS[1], URLS[0]], 2),
        (moved, [URLS[0], URLS[0], URLS[1]], 1),
    ):
        assert result == payload(mode=mode, urls=urls, index=index)
        assert [u.encode() for u in result["urls"]] == [u.encode() for u in urls]
        assert result is not source and result["urls"] is not source["urls"]
    appended["urls"].clear()
    removed["urls"].clear()
    moved["urls"].clear()
    assert source == original


def test_every_small_move_preserves_original_selected_occurrence_even_all_duplicates():
    for count in range(1, 6):
        for urls in ([URLS[0]] * count, [URLS[i % 2] for i in range(count)]):
            for selected in range(1, count + 1):
                source = payload(urls=urls, index=selected)
                for start in range(1, count + 1):
                    for final in range(1, count + 1):
                        result = move_playlist(source, start, final)
                        # Independent interval mapping, without looking up URL values.
                        expected = selected
                        if selected == start:
                            expected = final
                        elif start < selected <= final:
                            expected -= 1
                        elif final <= selected < start:
                            expected += 1
                        assert result["index"] == expected, (count, selected, start, final)
                        surviving = urls[:start - 1] + urls[start:]
                        assert result["urls"] == (surviving[:final - 1] + [urls[start - 1]]
                                                   + surviving[final - 1:])
                        assert source == payload(urls=urls, index=selected)


def test_every_small_removal_selects_next_neighbor_or_final_previous():
    for count in range(2, 6):
        urls = [URLS[i % 2] for i in range(count)]
        for selected in range(1, count + 1):
            for removed in range(1, count + 1):
                result = remove_playlist(payload(urls=urls, index=selected), removed)
                expected = selected
                if removed < selected:
                    expected -= 1
                elif removed == selected and selected == count:
                    expected -= 1
                assert result["index"] == expected
                assert result["urls"] == urls[:removed - 1] + urls[removed:]
    with pytest.raises(PlaylistError):
        remove_playlist(payload(urls=URLS[:1], index=1), 1)


@pytest.mark.parametrize("index", [True, False, 0, -1, 4, 1.0, "1", None])
def test_pure_indices_are_strict_integers_within_source_count(index):
    source = payload()
    for operation in (lambda: remove_playlist(source, index),
                      lambda: move_playlist(source, index, 1),
                      lambda: move_playlist(source, 1, index)):
        with pytest.raises(PlaylistError):
            operation()
    assert source == payload()


@pytest.mark.parametrize("urls", [[], (), tuple(URLS), URLS[0], None, [False], [1]])
def test_append_requires_nonempty_plain_url_list(urls):
    with pytest.raises(PlaylistError):
        append_playlist(payload(), urls)


@pytest.mark.parametrize("changes", [
    {"version": True}, {"index": True}, {"index": 0}, {"mode": "unknown"},
    {"metadata": "synthetic-secret"}, {"name": " padded "},
    {"urls": [URLS[0], URLS[1], "file:///synthetic-secret"]},
    {"urls": [URLS[0], URLS[1], "https://media.example.test/?q=" + "&" * 120000]},
])
def test_every_edit_validates_the_whole_source_including_an_item_to_be_removed(changes):
    source = payload(**changes)
    original = deepcopy(source)
    for operation in (lambda: append_playlist(source, URLS[:1]),
                      lambda: remove_playlist(source, 3),
                      lambda: move_playlist(source, 3, 3)):
        with pytest.raises((PlaylistError, QueueClientError)):
            operation()
    assert source == original


def test_count_boundary_and_invalid_appended_url_are_all_or_nothing():
    source = payload(urls=[URLS[0]] * (MAX_PLAYLIST_URLS - 1))
    assert len(append_playlist(source, URLS[:1])["urls"]) == MAX_PLAYLIST_URLS
    for urls in (URLS, [URLS[0], "https://user:synthetic-secret@media.example.test/audio"]):
        with pytest.raises((PlaylistError, QueueClientError)):
            append_playlist(source, urls)
    assert len(source["urls"]) == MAX_PLAYLIST_URLS - 1
