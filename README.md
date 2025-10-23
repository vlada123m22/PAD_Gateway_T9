# PAD_Gateway_T9# Mafia Platform

# PAD Gateway T9

## Overview

PAD Gateway T9 is a FastAPI-based API gateway for a multi-service game backend. It proxies requests to various microservices, provides authentication and caching, and exposes admin endpoints for service monitoring and log retrieval. The project is containerized using Docker and orchestrated with Docker Compose, supporting HAProxy load balancing and integration with the ELK stack for centralized logging.

## Features

- **API Gateway**: Proxies requests to game, user, town, character, voting, rumors, and shop services.
- **Authentication**: JWT-based user authentication and internal service token support.
- **Caching**: Redis-based response caching for GET endpoints.
- **Service Discovery**: Auto-discovers running Docker services for admin endpoints.
- **Admin Endpoints**: Download logs from all services as a ZIP, list running services, view gateway stats.
- **Logging**: Integrated with ELK stack (Elasticsearch, Logstash, Kibana, Filebeat).
- **Load Balancing**: HAProxy configuration for service routing and health checks.

## Architecture

- **gateway**: FastAPI app (`gateway.py`) running on port 8000.
- **HAProxy**: Load balancer for backend services.
- **ELK Stack**: Centralized logging (Elasticsearch, Logstash, Kibana, Filebeat).
- **Redis**: Caching layer for gateway responses.
- **Multiple Microservices**: User, game, town, character, voting, rumors, shop, roleplay.

## Getting Started

### Prerequisites

- Docker
- Docker Compose

### Setup

1. **Clone the repository**  
   Place all files in a directory.

2. **Environment Variables**  
   Create a `.env` file with required secrets and configuration (see `docker-compose.yml` for variables).

3. **Build and Start Services**
   ```sh
   docker-compose up --build
   ```

4. **Access Gateway**
   - API Gateway: [http://localhost:8000](http://localhost:8000)
   - HAProxy Stats: [http://localhost:8404/stats](http://localhost:8404/stats)
   - Kibana: [http://localhost:5601](http://localhost:5601)

### Main Endpoints

#### User Service

- `POST /api/users` — Create user
- `GET /api/users` — List users (cached)
- `GET /api/users/{user_id}` — Get user by ID (cached)

#### Game Service

- `POST /api/lobbies` — Create lobby
- `GET /api/lobbies` — List lobbies (cached)
- `POST /api/lobbies/{lobby_id}/join` — Join lobby
- `GET /api/lobbies/{lobby_id}` — Get lobby info (cached, authenticated)
- `PATCH /api/lobbies/{lobby_id}/state` — Update lobby state

#### Task Service

- `POST /api/tasks/assign` — Assign task (authenticated)
- `GET /api/tasks/view/{character_id}` — View tasks (authenticated, cached)
- `POST /api/tasks/complete/{task_id}/{character_id}` — Complete task (authenticated)

#### Voting Service

- `GET /api/voting/results/{lobby_id}` — Voting results (authenticated, cached)
- `POST /api/voting/cast` — Cast vote (authenticated)

#### Rumors Service

- `POST /api/rumors/generate` — Generate rumors (authenticated)
- `GET /api/rumors/{character_id}` — Get rumors for character (authenticated, cached)
- `GET /api/rumors` — List rumors (authenticated, cached)

#### Town Service

- `GET /api/town` — List towns (authenticated, cached)
- `GET /api/town/lobbies/{lobby_id}/locations/{location_id}/occupants` — Occupants (authenticated, cached)
- `POST /api/town/move` — Move (authenticated)
- `GET /api/town/movements` — Movements (authenticated, cached)
- `GET /api/town/phase/{lobby_id}` — Town phase (authenticated, cached)
- `POST /api/town/phase/{lobby_id}/toggle` — Toggle phase (admin only)

#### Character Service

- `GET /api/characters` — List characters (authenticated, cached)
- `GET /api/characters/user/{user_id}` — Get character by user (authenticated, cached)
- `PATCH /api/characters/user/{user_id}` — Update character (authenticated)
- `GET /api/characters/user/{user_id}/balance` — Get balance (authenticated, cached)
- `POST /api/characters/user/{user_id}/add-gold` — Add gold (internal/service only)
- `GET /api/characters/{character_id}` — Get character by ID (authenticated, cached)

#### Admin Endpoints

- `GET /api/logs/services` — List running services and containers
- `GET /api/logs/download` — Download logs from all/selected services as ZIP
- `GET /api/admin/stats` — Gateway stats (admin only)

## Configuration Files

- [`gateway.py`](gateway.py): FastAPI gateway source code
- [`docker-compose.yml`](docker-compose.yml): Service orchestration
- [`haproxy.cfg`](haproxy.cfg): HAProxy configuration
- [`nginx-proxy.conf`](nginx-proxy.conf): Nginx proxy config for task-service
- [`requirements.txt`](requirements.txt): Python dependencies
- [`Dockerfile`](Dockerfile): Gateway container build

## Logging & Monitoring

- **ELK Stack**: All Docker container logs are shipped via Filebeat to Logstash and stored in Elasticsearch. Kibana provides a web UI for log analysis.
- **HAProxy Stats**: Available at `/stats` endpoint.

## Notes

- Ensure Docker Desktop is running and accessible.
- Internal service requests require the `X-Internal-Service-Token` header.
- JWT secrets and other sensitive values should be set via environment variables.

## License

This project is intended for educational use. See individual microservice repositories for their respective licenses.


## Github workflow description

#### Branches and branches naming conventions

- main - the main branch of the project
- **naming convention for other branches:** `[<issue-name><issue-description>]`

#### Pushing

- pushing to feature branches: unrestricted
- pushing to main: not allowed

#### Pull requests & Merging strategy:

- A PR needs one approval before being merged (unless the user who is pushing has bybass permissions)
- Merging strategy: merge commit

### Create an index in Kibana ```docker-logs*```
