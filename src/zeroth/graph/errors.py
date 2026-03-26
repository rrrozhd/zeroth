"""Error types used by the graph package.

These errors are raised when something goes wrong with graph lifecycle
operations like publishing, archiving, or updating a graph.
"""


class GraphLifecycleError(ValueError):
    """Raised when you try to move a graph to a status it cannot go to.

    For example, you cannot publish a graph that is already archived,
    or revert a published graph back to draft.
    """

