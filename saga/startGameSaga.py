import asyncio
from brokerClient import brokerClient

saga_state = {}


def init_saga(lobby_id):
    saga_state[lobby_id] = {
        "character_valid": False,
        "lobby_locked": False,
        "completed": False
    }


async def check_saga_completion(lobby_id):
    saga = saga_state.get(lobby_id)
    if not saga:
        return

    if saga["character_valid"] and saga["lobby_locked"] and not saga["completed"]:
        saga["completed"] = True
        await brokerClient.publish("saga.start_game.completed", {
            "lobby_id": lobby_id
        })


async def handle_start_game(message):
    lobby_id = message["lobby_id"]
    init_saga(lobby_id)

    await brokerClient.publish("character.validate_players", {
        "lobby_id": lobby_id
    })

    await brokerClient.publish("game.lock_lobby", {
        "lobby_id": lobby_id
    })


async def handle_character_valid(message):
    lobby_id = message["lobby_id"]
    saga_state[lobby_id]["character_valid"] = True
    await check_saga_completion(lobby_id)


async def handle_lobby_locked(message):
    lobby_id = message["lobby_id"]
    saga_state[lobby_id]["lobby_locked"] = True
    await check_saga_completion(lobby_id)


async def handle_saga_failed(message):
    lobby_id = message["lobby_id"]
    reason = message.get("reason", "No reason provided")

    await brokerClient.publish("character.unreserve_players", {
        "lobby_id": lobby_id
    })

    await brokerClient.publish("game.unlock_lobby", {
        "lobby_id": lobby_id
    })

    await brokerClient.publish("saga.start_game.aborted", {
        "lobby_id": lobby_id,
        "error": reason
    })


async def start_saga_listener():
    await brokerClient.consume("saga.start_game", handle_start_game)
    await brokerClient.consume("saga.start_game.character_valid", handle_character_valid)
    await brokerClient.consume("saga.start_game.lobby_locked", handle_lobby_locked)
    await brokerClient.consume("saga.start_game.failed", handle_saga_failed)


asyncio.create_task(start_saga_listener())