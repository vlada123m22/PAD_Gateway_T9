# Sharded Redis Cache with Consistent Hashing

## Overview

This implementation scales your caching solution by distributing cache data across multiple Redis instances using **consistent hashing**. This approach provides:

- **Horizontal Scalability**: Add more Redis nodes to handle increased load
- **Even Distribution**: Keys are distributed evenly across shards
- **Minimal Disruption**: Adding/removing nodes only affects a small subset of keys
- **High Availability**: If one shard fails, only its keys are affected

## Architecture

```
┌─────────────┐
│   Gateway   │
│  Service    │
└──────┬──────┘
       │
       ├─── Consistent Hash Ring ───┐
       │                             │
       ▼                             ▼
┌──────────────┐            ┌──────────────┐
│ Redis Shard 1│            │ Redis Shard 2│
│  (Port 6379) │            │  (Port 6380) │
└──────────────┘            └──────────────┘
       │                             │
       └─────────────┬───────────────┘
                     ▼
              ┌──────────────┐
              │ Redis Shard 3│
              │  (Port 6381) │
              └──────────────┘
```

## How Consistent Hashing Works

### 1. Hash Ring Construction
- Each physical Redis node is mapped to multiple positions (virtual nodes) on a hash ring
- Default: 150 virtual nodes per physical node
- Virtual nodes ensure even distribution even with few physical nodes

### 2. Key Assignment
- Each cache key is hashed to produce a value on the ring
- The key is assigned to the first node clockwise from its hash position
- This ensures consistent assignment across requests

### 3. Node Addition/Removal
- Only keys between the new/removed node and its predecessor are affected
- ~1/N of keys move when adding/removing a node (where N = number of nodes)

## Configuration

### Environment Variables

```bash
# Comma-separated list of Redis shard URLs
CACHE_SHARD_URLS=redis://gateway_cache_1:6379,redis://gateway_cache_2:6379,redis://gateway_cache_3:6379

# Number of virtual nodes per physical node (higher = better distribution)
VIRTUAL_NODES=150

# Default cache TTL in seconds
CACHE_DEFAULT_TTL=15
```

### Scaling Up

To add more shards, update `docker-compose.yml`:

```yaml
gateway_cache_4:
  image: redis:7-alpine
  container_name: gateway_cache_4
  restart: unless-stopped
  command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
  ports:
    - "6382:6379"
  networks:
    - backend
```

Then update the gateway environment:

```yaml
gateway:
  environment:
    CACHE_SHARD_URLS: redis://gateway_cache_1:6379,redis://gateway_cache_2:6379,redis://gateway_cache_3:6379,redis://gateway_cache_4:6379
```

## API Endpoints

### Get Cache Statistics
```bash
GET /api/cache/stats
```

Response:
```json
{
  "cache_stats": {
    "total_requests": 1500,
    "hits": 1200,
    "misses": 300,
    "hit_rate_percent": 80.0,
    "sets": 300,
    "errors": 0,
    "shard_distribution": {
      "redis://gateway_cache_1:6379": 450,
      "redis://gateway_cache_2:6379": 400,
      "redis://gateway_cache_3:6379": 350
    },
    "shards_count": 3,
    "virtual_nodes": 150
  },
  "hash_ring_distribution": {
    "redis://gateway_cache_1:6379": 150,
    "redis://gateway_cache_2:6379": 150,
    "redis://gateway_cache_3:6379": 150
  },
  "shards": [
    {
      "url": "redis://gateway_cache_1:6379",
      "connected": true,
      "virtual_nodes": 150
    }
  ]
}
```

### Clear Cache
```bash
POST /api/cache/clear?pattern=gw:*
```

Response:
```json
{
  "cleared": 1500,
  "pattern": "gw:*",
  "shards_cleared": 3
}
```

## Response Headers

Each cached response includes headers showing cache behavior:

```
X-Cache: HIT|MISS|BYPASS
X-Cache-Shard: redis://gateway_cache_2:6379
```

## Performance Benefits

### Before Sharding (Single Redis)
- **Max Throughput**: ~50,000 ops/sec
- **Memory Limit**: 256MB
- **Single Point of Failure**: Yes

### After Sharding (3 Redis Nodes)
- **Max Throughput**: ~150,000 ops/sec (3x)
- **Memory Limit**: 768MB (3x)
- **Single Point of Failure**: Partial (2/3 available on failure)

## Monitoring

### Check Shard Health
```bash
# Check all shards are responding
docker exec gateway_cache_1 redis-cli ping
docker exec gateway_cache_2 redis-cli ping
docker exec gateway_cache_3 redis-cli ping
```

### Monitor Memory Usage
```bash
# Check memory usage per shard
docker exec gateway_cache_1 redis-cli INFO memory | grep used_memory_human
docker exec gateway_cache_2 redis-cli INFO memory | grep used_memory_human
docker exec gateway_cache_3 redis-cli INFO memory | grep used_memory_human
```

### View Keys Per Shard
```bash
# Count keys in each shard
docker exec gateway_cache_1 redis-cli DBSIZE
docker exec gateway_cache_2 redis-cli DBSIZE
docker exec gateway_cache_3 redis-cli DBSIZE
```

## Deployment

### Start Services
```bash
docker-compose up -d
```

### Verify Sharding
```bash
# Check gateway logs for shard initialization
docker logs gateway_container | grep "SHARDED CACHE"

# Expected output:
# [SHARDED CACHE] Connected to shard: redis://gateway_cache_1:6379
# [SHARDED CACHE] Connected to shard: redis://gateway_cache_2:6379
# [SHARDED CACHE] Connected to shard: redis://gateway_cache_3:6379
# [SHARDED CACHE] Hash ring distribution: {...}
```

### Test Cache Distribution
```bash
# Make several requests and check shard headers
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/users
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/lobbies
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/characters

# Check which shard handled each request
# Look for X-Cache-Shard header in responses
```

## Troubleshooting

### Shard Connection Failures
```bash
# Check if Redis containers are running
docker ps | grep gateway_cache

# Check Redis logs
docker logs gateway_cache_1
docker logs gateway_cache_2
docker logs gateway_cache_3

# Verify network connectivity
docker exec gateway_container ping gateway_cache_1
```

### Uneven Distribution
If one shard has significantly more keys:
- Increase `VIRTUAL_NODES` (e.g., 200-300)
- Restart gateway service
- Clear cache to redistribute

### Performance Issues
```bash
# Check Redis response times
docker exec gateway_cache_1 redis-cli --latency

# Check for memory evictions
docker exec gateway_cache_1 redis-cli INFO stats | grep evicted_keys

# Increase memory limit if needed
# Edit docker-compose.yml: --maxmemory 512mb
```

## Advanced Configuration

### Custom Memory Policies
```yaml
gateway_cache_1:
  command: >
    redis-server
    --maxmemory 512mb
    --maxmemory-policy allkeys-lru
    --maxmemory-samples 10
```

### Persistence (Optional)
```yaml
gateway_cache_1:
  command: >
    redis-server
    --maxmemory 256mb
    --maxmemory-policy allkeys-lru
    --save 900 1
    --save 300 10
  volumes:
    - redis_1_data:/data
```

## Best Practices

1. **Virtual Nodes**: Use 100-200 virtual nodes per physical node
2. **Memory Limits**: Set per-shard limits to prevent OOM
3. **Health Checks**: Monitor shard availability
4. **Gradual Scaling**: Add one shard at a time
5. **Testing**: Test cache distribution with `/api/cache/stats`
6. **Monitoring**: Track hit rates and shard distribution
7. **Eviction Policy**: Use `allkeys-lru` for cache workloads

## Migration from Single Redis

1. **Deploy sharded setup** alongside existing single Redis
2. **Update `CACHE_SHARD_URLS`** to include all shards
3. **Restart gateway** to activate sharding
4. **Monitor** cache stats for even distribution
5. **Remove old Redis** once stable

## Performance Tuning

### For Read-Heavy Workloads
- Increase shard count (4-6 nodes)
- Increase TTL for stable data
- Monitor hit rates per endpoint

### For Write-Heavy Workloads
- Use shorter TTLs
- Consider bypassing cache for frequently updated data
- Monitor set operation latency

### For Memory Constraints
- Reduce TTL values
- Increase shard count
- Use more aggressive eviction policies