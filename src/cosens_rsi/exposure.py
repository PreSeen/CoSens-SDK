"""Cross-job reservations around the unchanged local BundleRegistry."""

from .algorithms.hierarchical import fingerprint
from .algorithms.hierarchical_learning import BundleRegistry


def legacy_package(case, principal, project):
    # Legacy providers own task lineage. Keep both their declared lineage and
    # the content identity, so renaming a case/family cannot reset exposure.
    content = fingerprint({"task": case["task"], "inputs": case.get("inputs", {})})
    return {
        "principal": principal,
        "project": project,
        "lineage": content,
        "task": case["task"],
        "inputs": {},
        "exposure_keys": {"task:" + content, "declared-lineage:" + case["lineage_id"]},
    }


class GlobalBundleRegistry(BundleRegistry):
    def __init__(self, root, service, job_id, principal, cases):
        super().__init__(root)
        self.service, self.job_id = service, job_id
        self.packages = {
            c["id"]: legacy_package(c, principal, service.memory_project) for c in cases
        }

    def used_confirm(self, family):
        return super().used_confirm(family) | {
            cid
            for cid, p in self.packages.items()
            if not self.service._package_unexposed(p, self.job_id)
        }

    def reserve_confirm(self, family, candidate, task_ids):
        self.service._reserve_exposure(
            [self.packages[cid] for cid in task_ids], self.job_id, "confirm", candidate
        )
        super().reserve_confirm(family, candidate, task_ids)

    def guard(self, session, partition, ids):
        if partition in {"train", "screen"}:
            self.service._reserve_exposure(
                [self.packages[cid] for cid in ids],
                self.job_id,
                partition,
                session.manifest["sha256"],
            )
