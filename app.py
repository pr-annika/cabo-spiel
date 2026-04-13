# app.py
from gevent import monkey
monkey.patch_all()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import uuid

import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-nur-lokal')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# ─────────────────────────────────────────
# SPIELSTAND: Alle laufenden Spiele
# ─────────────────────────────────────────
games = {}  # { game_id: { ...spielstand... } }

# ─────────────────────────────────────────
# KARTEN-LOGIK
# ─────────────────────────────────────────
def create_deck():
    """Erstellt ein Cabo-Deck: 52 Karten, Werte 0-13."""
    deck = []
    # 2x Karte mit Wert 0 und 13
    for value in [0, 13]:
        for _ in range(2):
            deck.append({'value': value, 'action': None})
    # 4x Karten mit Wert 1-12
    for value in range(1, 13):
        action = None
        if value in [7, 8]:
            action = 'peek'    # eigene Karte anschauen
        elif value in [9, 10]:
            action = 'spy'     # Karte des Gegners anschauen
        elif value in [11, 12]:
            action = 'swap'    # Karte tauschen
        for _ in range(4):
            deck.append({'value': value, 'action': action})
    random.shuffle(deck)
    return deck

def card_display(card, hidden=False):
    """Gibt eine Karte als Dictionary zurück (für den Client)."""
    if hidden:
        return {'hidden': True}
    action_symbols = {'peek': '👁', 'spy': '🔍', 'swap': '🔄', None: ''}
    return {
        'hidden': False,
        'value': card['value'],
        'action': card['action'],
        'symbol': action_symbols[card['action']]
    }

# ─────────────────────────────────────────
# SPIEL-SETUP
# ─────────────────────────────────────────
def create_game():
    """Erstellt ein neues Spiel und gibt die game_id zurück."""
    game_id = str(uuid.uuid4())[:6].upper()
    games[game_id] = {
        'players': [],        # Liste von {sid, name, hand, total_score}
        'deck': [],
        'discard': [],
        'phase': 'waiting',   # waiting → peek_phase → playing → cabo_called → scoring
        'current_turn': 0,    # Index des aktiven Spielers
        'cabo_caller': None,  # sid des Cabo-Ansagers
        'drawn_card': None,   # Gezogene Karte (noch nicht platziert)
        'drawn_from': None,   # 'deck' oder 'discard'
        'pending_action': None, # Aktive Kartenaktion (peek/spy/swap)
        'round': 1,
    }
    return game_id

def get_game_state(game_id, for_player_sid):
    """
    Gibt den Spielstand zurück – aus Sicht eines bestimmten Spielers.
    Gegnerische Karten sind versteckt (außer bei Aktionen).
    """
    g = games[game_id]
    players_view = []
    for p in g['players']:
        hand = []
        for i, card in enumerate(p['hand']):
            # Eigene Karten: sichtbar wenn 'revealed', sonst versteckt
            if p['sid'] == for_player_sid:
                hand.append(card_display(card, hidden=not card.get('revealed', False)))
            else:
                # Gegner: immer versteckt (außer während spy-Aktion)
                hand.append(card_display(card, hidden=True))
        players_view.append({
            'sid': p['sid'],
            'name': p['name'],
            'hand': hand,
            'total_score': p['total_score'],
            'is_me': p['sid'] == for_player_sid,
        })

    current_player = g['players'][g['current_turn']] if g['players'] else None
    return {
        'game_id': game_id,
        'phase': g['phase'],
        'players': players_view,
        'discard_top': card_display(g['discard'][-1]) if g['discard'] else None,
        'deck_count': len(g['deck']),
        'current_turn_sid': current_player['sid'] if current_player else None,
        'cabo_caller': g['cabo_caller'],
        'drawn_card': card_display(g['drawn_card']) if g['drawn_card'] else None,
        'drawn_from': g['drawn_from'],
        'pending_action': g['pending_action'],
        'round': g['round'],
    }

def broadcast_state(game_id):
    """Sendet den Spielstand an alle Spieler (individuell angepasst)."""
    g = games[game_id]
    for p in g['players']:
        state = get_game_state(game_id, p['sid'])
        socketio.emit('game_state', state, to=p['sid'])

def next_turn(game_id):
    """Wechselt zum nächsten Spieler."""
    g = games[game_id]
    g['drawn_card'] = None
    g['drawn_from'] = None
    g['pending_action'] = None
    g['current_turn'] = (g['current_turn'] + 1) % len(g['players'])

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

# ─────────────────────────────────────────
# WEBSOCKET EVENTS
# ─────────────────────────────────────────

@socketio.on('create_game')
def on_create_game(data):
    """Spieler 1 erstellt ein neues Spiel."""
    game_id = create_game()
    name = data.get('name', 'Spieler 1')
    g = games[game_id]
    g['players'].append({
        'sid': request.sid,
        'name': name,
        'hand': [],
        'total_score': 0,
    })
    join_room(game_id)
    emit('game_created', {'game_id': game_id})
    broadcast_state(game_id)

@socketio.on('join_game')
def on_join_game(data):
    game_id = data.get('game_id', '').upper()
    name = data.get('name', 'Spieler 2')
    if game_id not in games:
        emit('error', {'msg': 'Spiel nicht gefunden!'})
        return
    g = games[game_id]

    # Schon im Spiel? (Reconnect mit neuer Socket-ID)
    existing = next((p for p in g['players'] if p['name'] == name), None)
    if existing:
        existing['sid'] = request.sid
        join_room(game_id)
        emit('joined_game', {'game_id': game_id})
        broadcast_state(game_id)
        return

    if len(g['players']) >= 2:
        emit('error', {'msg': 'Spiel ist bereits voll!'})
        return

    g['players'].append({
        'sid': request.sid,
        'name': name,
        'hand': [],
        'total_score': 0,
    })
    join_room(game_id)
    emit('joined_game', {'game_id': game_id})
    broadcast_state(game_id)

@socketio.on('start_game')
def on_start_game(data):
    """Startet das Spiel (nur wenn beide Spieler da sind)."""
    game_id = data.get('game_id')
    g = games[game_id]
    if len(g['players']) < 2:
        emit('error', {'msg': 'Es braucht 2 Spieler!'})
        return

    # Deck erstellen und Karten austeilen
    g['deck'] = create_deck()
    for p in g['players']:
        p['hand'] = []
        for _ in range(4):
            card = g['deck'].pop()
            card['revealed'] = False  # Alle Karten zunächst verdeckt
            p['hand'].append(card)

    # Erste Karte auf Ablagestapel
    g['discard'] = [g['deck'].pop()]

    # Peek-Phase: Jeder schaut sich 2 eigene Karten an
    g['phase'] = 'peek_phase'

    # Karten 0 und 1 (Index) für jeden Spieler kurz aufdecken
    for p in g['players']:
        p['hand'][0]['revealed'] = True
        p['hand'][1]['revealed'] = True

    broadcast_state(game_id)

@socketio.on('peek_done')
def on_peek_done(data):
    """Spieler hat seine 2 Startkarten angeschaut."""
    game_id = data.get('game_id')
    g = games[game_id]
    player = next((p for p in g['players'] if p['sid'] == request.sid), None)
    if not player:
        return

    # Karten wieder verdecken
    for card in player['hand']:
        card['revealed'] = False

    # Prüfen ob beide Spieler fertig sind
    player['peek_done'] = True
    if all(p.get('peek_done') for p in g['players']):
        g['phase'] = 'playing'
        g['current_turn'] = 0

    broadcast_state(game_id)

@socketio.on('draw_from_deck')
def on_draw_deck(data):
    """Spieler zieht vom Nachziehstapel."""
    game_id = data.get('game_id')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['drawn_card'] is not None:
        return

    # Nachziehstapel auffüllen falls nötig
    if not g['deck']:
        top = g['discard'].pop()
        g['deck'] = g['discard']
        random.shuffle(g['deck'])
        g['discard'] = [top]

    card = g['deck'].pop()
    g['drawn_card'] = card
    g['drawn_from'] = 'deck'
    broadcast_state(game_id)

@socketio.on('draw_from_discard')
def on_draw_discard(data):
    """Spieler nimmt die oberste Karte vom Ablagestapel."""
    game_id = data.get('game_id')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['drawn_card'] is not None:
        return
    if not g['discard']:
        return

    card = g['discard'].pop()
    g['drawn_card'] = card
    g['drawn_from'] = 'discard'
    broadcast_state(game_id)

@socketio.on('replace_card')
def on_replace_card(data):
    """Spieler tauscht gezogene Karte gegen eine Handkarte."""
    game_id = data.get('game_id')
    card_index = data.get('card_index')  # Index in der eigenen Hand
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['drawn_card'] is None:
        return

    old_card = current['hand'][card_index]
    new_card = g['drawn_card']
    new_card['revealed'] = False

    # Tausch durchführen
    current['hand'][card_index] = new_card
    g['discard'].append(old_card)
    g['drawn_card'] = None
    g['drawn_from'] = None

    next_turn(game_id)
    broadcast_state(game_id)

@socketio.on('discard_drawn')
def on_discard_drawn(data):
    """Spieler wirft die gezogene Karte ab (ohne Tausch, Aktion optional nutzbar)."""
    game_id = data.get('game_id')
    use_action = data.get('use_action', False)
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['drawn_card'] is None:
        return

    card = g['drawn_card']
    g['discard'].append(card)
    g['drawn_card'] = None

    if use_action and card['action'] and g['drawn_from'] == 'deck':
        g['pending_action'] = card['action']
        broadcast_state(game_id)
    else:
        next_turn(game_id)
        broadcast_state(game_id)

@socketio.on('use_peek')
def on_use_peek(data):
    """Peek-Aktion: Spieler schaut eine eigene Karte an."""
    game_id = data.get('game_id')
    card_index = data.get('card_index')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['pending_action'] != 'peek':
        return

    current['hand'][card_index]['revealed'] = True
    g['pending_action'] = None

    # Nach kurzer Zeit wieder verdecken (client-seitig gesteuert)
    broadcast_state(game_id)

@socketio.on('peek_card_done')
def on_peek_card_done(data):
    """Spieler hat die Peek-Karte gesehen, wieder verdecken."""
    game_id = data.get('game_id')
    card_index = data.get('card_index')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid:
        return
    current['hand'][card_index]['revealed'] = False
    next_turn(game_id)
    broadcast_state(game_id)

@socketio.on('use_spy')
def on_use_spy(data):
    """Spy-Aktion: Spieler schaut eine Karte des Gegners an."""
    game_id = data.get('game_id')
    target_sid = data.get('target_sid')
    card_index = data.get('card_index')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['pending_action'] != 'spy':
        return

    target = next((p for p in g['players'] if p['sid'] == target_sid), None)
    if not target:
        return

    # Karte kurz für den aktiven Spieler sichtbar machen
    target['hand'][card_index]['spied_by'] = request.sid
    g['pending_action'] = None

    # Jeden Spieler individuell updaten
    for p in g['players']:
        state = get_game_state(game_id, p['sid'])
        # Gespitzte Karte für Spion sichtbar machen
        if p['sid'] == request.sid:
            target_idx = next(i for i, pl in enumerate(g['players']) if pl['sid'] == target_sid)
            state['players'][target_idx]['hand'][card_index] = card_display(target['hand'][card_index])
        socketio.emit('game_state', state, to=p['sid'])

@socketio.on('spy_done')
def on_spy_done(data):
    """Spion hat die Karte gesehen."""
    game_id = data.get('game_id')
    target_sid = data.get('target_sid')
    card_index = data.get('card_index')
    g = games[game_id]
    target = next((p for p in g['players'] if p['sid'] == target_sid), None)
    if target:
        target['hand'][card_index].pop('spied_by', None)
    next_turn(game_id)
    broadcast_state(game_id)

@socketio.on('use_swap')
def on_use_swap(data):
    """Swap-Aktion: Tauscht eine eigene Karte mit einer Karte des Gegners."""
    game_id = data.get('game_id')
    my_index = data.get('my_index')
    target_sid = data.get('target_sid')
    target_index = data.get('target_index')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid or g['pending_action'] != 'swap':
        return

    target = next((p for p in g['players'] if p['sid'] == target_sid), None)
    if not target:
        return

    # Tausch (revealed-Status zurücksetzen)
    my_card = current['hand'][my_index]
    their_card = target['hand'][target_index]
    my_card['revealed'] = False
    their_card['revealed'] = False

    current['hand'][my_index] = their_card
    target['hand'][target_index] = my_card
    g['pending_action'] = None

    next_turn(game_id)
    broadcast_state(game_id)

@socketio.on('call_cabo')
def on_call_cabo(data):
    """Spieler sagt Cabo an."""
    game_id = data.get('game_id')
    g = games[game_id]
    current = g['players'][g['current_turn']]
    if current['sid'] != request.sid:
        return
    if g['cabo_caller'] is not None:
        return  # Cabo kann nur einmal pro Runde gerufen werden

    g['cabo_caller'] = request.sid
    g['phase'] = 'cabo_called'
    next_turn(game_id)
    broadcast_state(game_id)

@socketio.on('end_round')
def on_end_round(data):
    """Runde beenden und Punkte zählen (nach letztem Zug nach Cabo)."""
    game_id = data.get('game_id')
    g = games[game_id]

    # Alle Karten aufdecken
    for p in g['players']:
        for card in p['hand']:
            card['revealed'] = True

    # Punkte berechnen
    scores = {p['sid']: sum(c['value'] for c in p['hand']) for p in g['players']}
    min_score = min(scores.values())
    winner_sid = min(scores, key=scores.get)

    cabo_caller = g['cabo_caller']
    cabo_score = scores.get(cabo_caller, 0)

    # Kamikaze prüfen: 2x Wert 12 und 2x Wert 13
    kamikaze_player = None
    for p in g['players']:
        vals = [c['value'] for c in p['hand']]
        if vals.count(12) >= 2 and vals.count(13) >= 2:
            kamikaze_player = p['sid']

    round_results = []
    for p in g['players']:
        s = scores[p['sid']]
        points = s  # Standardfall

        if kamikaze_player:
            if p['sid'] == kamikaze_player:
                points = 0
            else:
                points = 50
        elif p['sid'] == winner_sid:
            # Gewinner bekommt 0 Punkte — außer Cabo-Ansager hat nicht gewonnen
            if cabo_caller and p['sid'] != cabo_caller:
                points = 0
            elif cabo_caller and p['sid'] == cabo_caller:
                points = 0  # Cabo-Ansager hat gewonnen: 0 Punkte
        
        # Cabo-Strafe: Ansager hat nicht die wenigsten Punkte
        if cabo_caller and p['sid'] == cabo_caller and cabo_score > min_score:
            points = s + 5

        p['total_score'] += points
        # Genau 100 Punkte → auf 50 reduziert
        if p['total_score'] == 100:
            p['total_score'] = 50

        round_results.append({
            'name': p['name'],
            'hand_score': s,
            'round_points': points,
            'total_score': p['total_score'],
        })

    g['phase'] = 'scoring'

    # Spielende prüfen (101+ Punkte)
    game_over = any(p['total_score'] >= 101 for p in g['players'])

    socketio.emit('round_over', {
        'results': round_results,
        'game_over': game_over,
        'kamikaze': kamikaze_player is not None,
    }, to=game_id)

    broadcast_state(game_id)

@socketio.on('next_round')
def on_next_round(data):
    """Neue Runde starten."""
    game_id = data.get('game_id')
    g = games[game_id]

    # Gewinner der letzten Runde wird Startspieler
    last_scores = {p['sid']: sum(c['value'] for c in p['hand']) for p in g['players']}
    winner_sid = min(last_scores, key=last_scores.get)
    winner_idx = next(i for i, p in enumerate(g['players']) if p['sid'] == winner_sid)

    g['deck'] = create_deck()
    for p in g['players']:
        p['hand'] = []
        p['peek_done'] = False
        for _ in range(4):
            card = g['deck'].pop()
            card['revealed'] = False
            p['hand'].append(card)

    g['discard'] = [g['deck'].pop()]
    g['phase'] = 'peek_phase'
    g['current_turn'] = winner_idx
    g['cabo_caller'] = None
    g['drawn_card'] = None
    g['pending_action'] = None
    g['round'] += 1

    # Startkarten aufdecken
    for p in g['players']:
        p['hand'][0]['revealed'] = True
        p['hand'][1]['revealed'] = True

    broadcast_state(game_id)

@socketio.on('disconnect')
def on_disconnect():
    for game_id, g in list(games.items()):
        for p in g['players']:
            if p['sid'] == request.sid:
                socketio.emit('player_left', {'name': p['name']}, to=game_id)
                # Spieler NICHT entfernen — er kann reconnecten
                break

# ─────────────────────────────────────────
# START
# ─────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('RENDER') is None  # debug nur lokal
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, allow_unsafe_werkzeug=True)

# Notizen
# create_deck() — baut das komplette Cabo-Deck mit allen 52 Karten und weist den 7/8/9/10/11/12ern ihre Aktionen zu.
# get_game_state() — das ist die wichtigste Funktion: sie erstellt den Spielstand aus Sicht eines bestimmten Spielers. Deine eigenen Karten siehst du (wenn aufgedeckt), die des Gegners nicht — genau wie im echten Spiel.
# broadcast_state() — sendet nach jeder Aktion den aktuellen Stand an beide Spieler (aber jeder bekommt seine eigene "Sicht").
# Die @socketio.on(...)-Funktionen sind die Aktionen, die ein Spieler auslösen kann — ziehen, ablegen, Cabo rufen usw.
