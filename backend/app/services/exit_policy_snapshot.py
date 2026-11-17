from dataclasses import dataclass, fields
from math import isfinite

from app.engine.exits import AtrStopPolicy, AtrTrailingPolicy, FixedSlTpPolicy


POLICY_TYPES = {kind.policy_id: kind for kind in (FixedSlTpPolicy, AtrStopPolicy, AtrTrailingPolicy)}


@dataclass(frozen=True)
class ExitPolicySnapshot:
    policy_id: str
    policy_version: str
    parameters: tuple[tuple[str, int | float | None], ...]

    def restore(self):
        if not isinstance(self.policy_id, str) or not isinstance(self.policy_version, str):
            raise ValueError("Exit policy identity must be a string")
        kind = POLICY_TYPES.get(self.policy_id)
        if kind is None or self.policy_version != kind.version:
            raise ValueError('Unsupported exit policy identity or version')
        defaults = {field.name: field.default for field in fields(kind) if field.init and field.name not in ('policy_id', 'version')}
        expected = set(defaults)
        parameters = dict(self.parameters)
        if set(parameters) != expected or len(parameters) != len(self.parameters):
            raise ValueError('Exact saved exit policy parameters required')
        for name, value in parameters.items():
            if name == 'period':
                if type(value) is not int or value <= 0:
                    raise ValueError('Positive integer exit period required')
            elif value is None:
                if defaults[name] is not None:
                    raise ValueError('Required exit parameter unavailable')
            elif type(value) not in (int, float) or not isfinite(value):
                raise ValueError('Finite exit policy parameter required')
        return kind(**parameters)

    def to_payload(self):
        self.restore()
        return dict(version=1, policy_id=self.policy_id, policy_version=self.policy_version,
                    parameters=dict(self.parameters))

    @classmethod
    def capture(cls, policy):
        if type(policy) not in POLICY_TYPES.values():
            raise ValueError('Unsupported exit policy implementation')
        parameters = tuple(sorted((field.name, getattr(policy, field.name)) for field in fields(policy)
                                  if field.init and field.name not in ('policy_id', 'version')))
        result = cls(policy.policy_id, policy.version, parameters)
        result.restore()
        return result

    @classmethod
    def from_payload(cls, payload):
        if (not isinstance(payload, dict) or set(payload) != {'version', 'policy_id', 'policy_version', 'parameters'}
                or type(payload['version']) is not int or payload['version'] != 1
                or not isinstance(payload['parameters'], dict)):
            raise ValueError('Invalid saved exit policy')
        result = cls(payload['policy_id'], payload['policy_version'], tuple(sorted(payload['parameters'].items())))
        result.restore()
        return result
