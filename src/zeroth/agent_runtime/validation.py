"""Output validation for agent responses.

After the AI model returns a response, this module checks that the
response matches the expected output format and converts it into a
typed Pydantic model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from zeroth.agent_runtime.errors import AgentOutputValidationError
from zeroth.agent_runtime.provider import ProviderResponse


class OutputValidator:
    """Checks that the AI model's response matches the expected output shape.

    Takes the raw response from the provider and tries to parse it into
    the Pydantic model you defined. Raises AgentOutputValidationError
    if the response does not fit.
    """

    def validate(
        self,
        output_model: type[BaseModel],
        response: ProviderResponse | Any,
    ) -> BaseModel:
        """Parse the provider response into the expected output model.

        Extracts the payload from the response, then validates it against
        the output model. Returns a validated Pydantic instance.
        """
        payload = self._extract_payload(response)
        try:
            return output_model.model_validate(payload)
        except ValidationError as exc:
            raise AgentOutputValidationError(str(exc)) from exc

    def _extract_payload(self, response: ProviderResponse | Any) -> Any:
        """Pull the usable data out of a provider response.

        Handles Pydantic models, dicts, and JSON strings. Raises
        AgentOutputValidationError if the content cannot be parsed.
        """
        content = response.content if isinstance(response, ProviderResponse) else response
        if isinstance(content, BaseModel):
            return content.model_dump(mode="json")
        if isinstance(content, Mapping):
            return dict(content)
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError as exc:
                raise AgentOutputValidationError("provider content is not valid JSON") from exc
        raise AgentOutputValidationError(f"unsupported provider payload type: {type(content)!r}")
