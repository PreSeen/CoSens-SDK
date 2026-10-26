"""Small JSON result helpers that preserve server fields."""


class Result(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def result(value):
    if isinstance(value, dict):
        return Result({key: result(item) for key, item in value.items()})
    if isinstance(value, list):
        return [result(item) for item in value]
    return value
