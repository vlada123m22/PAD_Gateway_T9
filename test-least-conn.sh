#!/bin/bash

echo "Testing Least Connections Algorithm"
echo "===================================="

# Start 10 long-running requests (simulating slow requests to replica 1)
for i in {1}
do
  curl -s "http://localhost:8180/health" &
  echo "Started slow request $i"
done

sleep 1

# Send 100 quick requests - these should go to replicas 2 and 3
echo ""
echo "Sending 10 fast requests..."
for i in {1..5}
do
  curl -s http://localhost:8180/health > /dev/null
  echo "Fast request $i sent"
done

echo ""
echo "Check HAProxy stats: replicas 2 and 3 should have more requests"