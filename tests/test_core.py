from pathlib import Path
from spindle.catalog import Catalog, CatalogError, norm, relevance, apple_song_id
from spindle.queue_store import QueueStore, QueueError
from spindle.models import Track
from spindle.commands import CommandEngine
from spindle.settings import Settings
import pytest

AMEN = Track('AMEN', 'Beyoncé', 'COWBOY CARTER', 180000, '123456789',
             'https://music.apple.com/us/album/amen/000?i=123456789')


def test_accent_tolerant_match():
    assert norm('Beyoncé') == 'beyonce'
    assert relevance('AMEN Beyonce', AMEN) > .9
    assert apple_song_id('https://music.apple.com/us/album/cowboy-carter/1234?i=123456789') == '123456789'
    assert apple_song_id('https://music.apple.com/us/album/123456789') is None


def test_catalog_selection_and_direct_link():
    item = {'kind':'song','trackName':'AMEN','artistName':'Beyoncé','collectionName':'COWBOY CARTER',
            'trackTimeMillis':180000,'trackId':123456789,
            'trackViewUrl':'https://music.apple.com/us/song/amen/123456789'}
    c = Catalog(fetch=lambda url: {'results':[item]})
    assert c.best('AMEN Beyonce').artist == 'Beyoncé'
    assert c.best('https://music.apple.com/us/song/amen/123456789').track_id == '123456789'


def test_queue_is_persistent_and_enforces_limits(tmp_path):
    db = tmp_path / 'requests.db'
    q = QueueStore(db)
    assert q.add(AMEN, '1','fan', cooldown_seconds=0)[1] == 1
    with pytest.raises(QueueError, match='already'):
        q.add(AMEN, '2','another', cooldown_seconds=0)
    q.close()
    q2 = QueueStore(db)
    assert q2.pending()[0].artist == 'Beyoncé'
    assert q2.remove_own('1')
    assert not q2.pending()
    q2.close()


def test_command_engine_distinguishes_pending_from_playing(tmp_path):
    q = QueueStore(tmp_path / 'requests.db')
    s = Settings(cooldown_seconds=0)
    catalog = type('CatalogStub',(),{'best':lambda self,q:AMEN})()
    engine = CommandEngine(q,catalog,s)
    assert 'not connected' in engine.handle('1','fan','!song')
    engine.set_current(Track('Fantasy', 'Mariah Carey', playing=True))
    assert 'Mariah Carey' in engine.handle('1','fan','!song')
    response = engine.handle('1','fan','!sr AMEN Beyonce')
    assert 'Spindle request queue' in response
    assert 'Apple Music Up Next yet' in response
    assert 'Beyoncé' in engine.handle('2','other','!queue')
    assert engine.handle('1','fan','!unrequest').endswith('was removed.')
    q.close()


def test_nonmod_cannot_disable(tmp_path):
    q = QueueStore(tmp_path / 'requests.db')
    s = Settings(cooldown_seconds=0)
    catalog = type('CatalogStub',(),{'best':lambda self,q: AMEN})()
    engine = CommandEngine(q,catalog,s)
    engine.handle('1','viewer','!sr off')
    assert s.requests_enabled is True
    q.close()
