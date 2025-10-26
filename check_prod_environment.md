# Production Environment Debugging Checklist

## Issue
Production is missing activity heart rate data (`heart_rate_series` is empty) while dev works correctly.

## What We Know

### Dev Environment (macOS)
- Python: 3.11.13
- garminconnect: 0.1.50 (installed)
- Requirements: >=0.1.61
- `activityDetailMetrics`: ✅ Working (1565 entries)
- HR data extracted: ✅ Yes

### Prod Environment (Windows 11)
- `activityDetailMetrics`: ❌ Empty or missing structure
- HR data extracted: ❌ No
- Needs verification: Everything below

## Checks Needed on Prod Server

### 1. Check Python Version
```bash
python --version
```

### 2. Check Installed Package Versions
```bash
pip list | grep -E "(garminconnect|garth|requests)"
```

Expected to find:
- garminconnect >= 0.1.61
- garth >= 0.5.17
- requests >= 2.31.0

### 3. Check Actual Installed Code
```bash
python -c "import garminconnect; print(garminconnect.__file__)"
```

### 4. Force Upgrade/Reinstall
```bash
pip install --upgrade garminconnect>=0.1.61
pip install --upgrade garth>=0.5.17
pip install --upgrade requests>=2.31.0
```

### 5. Check if There Are Multiple Python Environments
```bash
which python
python -m pip list
```

### 6. Test Activity Details Extraction
Create a test script on prod to see what the API actually returns:

```python
import json
from garminconnect import Garmin
import os

api = Garmin(os.getenv('GARMIN_EMAIL'), os.getenv('GARMIN_PASSWORD'))
api.login()

activity_details = api.get_activity_details("20802404048")

print(f"Has activityDetailMetrics: {'activityDetailMetrics' in activity_details}")

if 'activityDetailMetrics' in activity_details:
    metrics = activity_details['activityDetailMetrics']
    print(f"Type: {type(metrics)}")
    print(f"Length: {len(metrics) if hasattr(metrics, '__len__') else 'N/A'}")
    
if 'metricDescriptors' in activity_details:
    descriptors = activity_details['metricDescriptors']
    print(f"Metric descriptors count: {len(descriptors)}")
    for desc in descriptors[:5]:
        print(f"  - {desc.get('key')} at index {desc.get('metricsIndex')}")
```

### 7. Check Collector Logs
Look for error messages or warnings about missing data:
- "No metricDescriptors found"
- "Could not find HR and timestamp positions"
- "No activityDetailMetrics data"

### 8. Verify Environment Variables
```bash
cat .env | grep GARMIN
```

## Potential Issues

1. **Wrong Version**: Prod might have garminconnect < 0.1.50
2. **Cached Package**: Old cached version not upgrading
3. **Virtual Environment**: Running in different venv than expected
4. **Path Issues**: Python finding wrong package installation
5. **API Changes**: Garmin API change affecting certain requests
6. **Network Issues**: Garmin throttling or blocking Windows requests differently

## Next Steps After Investigation

Once we know the actual versions and what prod is receiving, we can:
1. Pin exact versions in requirements.txt
2. Update prod to match dev environment
3. Fix any compatibility issues identified
