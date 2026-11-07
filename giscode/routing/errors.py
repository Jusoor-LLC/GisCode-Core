class RoutingError(Exception):
    pass


class ModelUnavailableError(RoutingError):
    pass


class ContextAssemblyError(RoutingError):
    pass


class InvalidRoleError(RoutingError):
    pass


class PolicyError(RoutingError):
    pass
