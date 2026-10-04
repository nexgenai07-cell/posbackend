class BranchScopedQuerysetMixin:
    """
    Restricts a viewset's queryset to the requesting staff's branch. Every
    branch-scoped model's viewset should use this — retrofitting branch
    scoping later, once a second branch actually exists, is exactly the
    expensive migration this project's plan wants to avoid needing.
    """

    def get_queryset(self):
        return super().get_queryset().filter(branch=self.request.user.branch)
