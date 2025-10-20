#!/bin/bash

echo "Waiting for Kibana to be ready..."
until curl -s http://localhost:5601/api/status | grep -q '"state":"green"'; do
  echo "Waiting for Kibana..."
  sleep 5
done

echo "Kibana is ready!"
echo ""
echo "Creating index patterns..."

# Create index pattern for logs
curl -X POST "http://localhost:5601/api/saved_objects/index-pattern/logs-*" \
  -H 'kbn-xsrf: true' \
  -H 'Content-Type: application/json' \
  -d '{
    "attributes": {
      "title": "logs-*",
      "timeFieldName": "@timestamp"
    }
  }'

# Create index pattern for metrics
curl -X POST "http://localhost:5601/api/saved_objects/index-pattern/metricbeat-*" \
  -H 'kbn-xsrf: true' \
  -H 'Content-Type: application/json' \
  -d '{
    "attributes": {
      "title": "metricbeat-*",
      "timeFieldName": "@timestamp"
    }
  }'

echo ""
echo "✓ Index patterns created!"
echo ""
echo "Access Kibana at: http://localhost:5601"
echo ""
echo "Pre-configured dashboards:"
echo "1. Discover > Select 'logs-*' index pattern"
echo "2. Dashboard > Create new dashboard"
echo "3. Add visualizations for your services"