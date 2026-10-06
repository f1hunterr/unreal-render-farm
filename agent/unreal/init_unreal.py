# Unreal runs init_unreal.py from every folder on UE_PYTHONPATH at startup. The agent points
# UE_PYTHONPATH here, so the URF executor class is registered without changing your project.
import urf_executor  # noqa: F401
