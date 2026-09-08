#!/bin/bash
# Opens the dashboard on a real recorded experiment trial
# (E1 P2_ML_RL seed2: detected s1-s3, rerouted via the RL policy,
#  verified SUCCESS in 21.7s). Stop it with Ctrl+C.
cd "$(dirname "$0")"
echo ""
echo "  Starting dashboard..."
echo "  When you see 'Application startup complete', open:"
echo ""
echo "      http://127.0.0.1:8000"
echo ""
AI_SDN_CONFIG=config/demo_config.yaml venv-ml/bin/uvicorn backend.api:app --port 8000
