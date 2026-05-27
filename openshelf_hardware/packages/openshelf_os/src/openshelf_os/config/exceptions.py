from typing import Any, Dict, List, Optional


class ConfigError(Exception):
    pass


class ConfigValidationError(ConfigError):
    def __init__(self, errors: List[Dict[str, Any]], path: Optional[str] = None):
        self.errors = errors
        self.path = path
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        lines = ["Configuration validation failed:"]
        if self.path is not None:
            lines.append(f"  File: {self.path}")
        lines.append("")
        for err in self.errors:
            loc = ".".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "unknown error")
            lines.append(f"  - {loc}: {msg}")
        return "\n".join(lines)


class ConfigMissingError(ConfigError):
    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Configuration file not found: {path}")
