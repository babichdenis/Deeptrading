from dataclasses import dataclass, field


@dataclass
class EntryPause:
    manual: bool = False
    blockers: set[tuple] = field(default_factory=set)

    @property
    def paused(self):
        return self.manual or bool(self.blockers)

    @property
    def reasons(self):
        return sorted(({key[0] for key in self.blockers}) | ({'manual'} if self.manual else set()))

    def set_blocker(self, reason, active, *identity):
        key = (reason, *identity)
        if active:
            self.blockers.add(key)
        else:
            self.blockers.discard(key)
