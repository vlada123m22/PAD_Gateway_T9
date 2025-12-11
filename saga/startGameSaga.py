from utils.message_broker import MessageBroker
import threading

broker = MessageBroker()
saga_state = {}


def initialize_saga_state(lobby_id):
    saga_state[lobby_id] = {
        "character_valid": False,
        "lobby_locked": False,
        "completed": False
    }


def check_saga_completion(lobby_id):
    saga = saga_state.get(lobby_id)
    if not saga:
        return

    if saga["character_valid"] and saga["lobby_locked"] and not saga["completed"]:
        saga["completed"] = True
        broker.publish("saga.start_game.completed", {
            "lobby_id": lobby_id
        })


def handle_start_game(message):
    lobby_id = message["lobby_id"]
    initialize_saga_state(lobby_id)

    broker.publish("character.validate_players", {
        "lobby_id": lobby_id
    })

    broker.publish("game.lock_lobby", {
        "lobby_id": lobby_id
    })


def handle_character_valid(message):
    lobby_id = message["lobby_id"]
    saga_state[lobby_id]["character_valid"] = True
    check_saga_completion(lobby_id)


def handle_lobby_locked(message):
    lobby_id = message["lobby_id"]
    saga_state[lobby_id]["lobby_locked"] = True
    check_saga_completion(lobby_id)


def handle_saga_failed(message):
    lobby_id = message["lobby_id"]
    reason = message.get("reason", "No reason provided")

    broker.publish("character.unreserve_players", {
        "lobby_id": lobby_id
    })

    broker.publish("game.unlock_lobby", {
        "lobby_id": lobby_id
    })

    broker.publish("saga.start_game.aborted", {
        "lobby_id": lobby_id,
        "error": reason
    })


def start_saga_listener():
    broker.subscribe("saga.start_game", handle_start_game)
    broker.subscribe("saga.start_game.character_valid", handle_character_valid)
    broker.subscribe("saga.start_game.lobby_locked", handle_lobby_locked)
    broker.subscribe("saga.start_game.failed", handle_saga_failed)


threading.Thread(target=start_saga_listener, daemon=True).start()