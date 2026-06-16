"""System resource monitor — CPU, RAM, GPU (NVIDIA)."""
import psutil

try:
    import ctypes, pynvml
    # pynvml mặc định tìm DLL sai path trên Windows
    # Load thủ công từ System32
    pynvml.nvmlLib = ctypes.CDLL(r"C:\Windows\System32\nvml.dll")
    pynvml.nvmlInit()
    _handle   = pynvml.nvmlDeviceGetHandleByIndex(0)
    _gpu_name = pynvml.nvmlDeviceGetName(_handle)
    if isinstance(_gpu_name, bytes):
        _gpu_name = _gpu_name.decode()
    NVML_OK = True
except Exception:
    NVML_OK   = False
    _gpu_name = "N/A"


def get_gpu_name() -> str:
    return _gpu_name


def get_stats() -> dict:
    vm = psutil.virtual_memory()
    stats = {
        "cpu_pct":        round(psutil.cpu_percent(interval=None), 1),
        "ram_used_gb":    round(vm.used  / 1e9, 1),
        "ram_total_gb":   round(vm.total / 1e9, 1),
        "ram_pct":        vm.percent,
        "gpu_pct":        0,
        "gpu_temp_c":     0,
        "vram_used_gb":   0.0,
        "vram_total_gb":  12.0,
    }
    if NVML_OK:
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(_handle)
            mem  = pynvml.nvmlDeviceGetMemoryInfo(_handle)
            temp = pynvml.nvmlDeviceGetTemperature(_handle, pynvml.NVML_TEMPERATURE_GPU)
            stats.update({
                "gpu_pct":       util.gpu,
                "gpu_temp_c":    temp,
                "vram_used_gb":  round(mem.used  / 1e9, 1),
                "vram_total_gb": round(mem.total / 1e9, 1),
            })
        except Exception:
            pass
    return stats
