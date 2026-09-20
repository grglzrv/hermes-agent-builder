class AgentBuilderError(Exception): pass
class ValidationError(AgentBuilderError): pass
class AuthorizationError(AgentBuilderError): pass
class NotFoundError(AgentBuilderError): pass
class ConflictError(AgentBuilderError): pass
class ApprovalRequired(AgentBuilderError): pass
