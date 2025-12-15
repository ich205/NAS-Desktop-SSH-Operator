from jfo.core.nfo import parse_nfo


def test_parse_nfo_movie_basic():
    xml = """<?xml version='1.0' encoding='UTF-8'?>
<movie>
  <title>Inception</title>
  <year>2010</year>
  <imdbid>tt1375666</imdbid>
</movie>
"""
    info = parse_nfo(xml)
    assert info.title == "Inception"
    assert info.year == 2010
    assert info.imdbid == "tt1375666"


def test_parse_nfo_imdb_in_id_field():
    xml = """
<movie>
  <title>Alien</title>
  <year>1979</year>
  <id>tt0078748</id>
</movie>
"""
    info = parse_nfo(xml)
    assert info.imdbid == "tt0078748"
