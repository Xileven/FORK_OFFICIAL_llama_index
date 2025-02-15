"""Google's hosted Gemini API."""

import os
import warnings
import uuid
from typing import TYPE_CHECKING, Union, List, Any, Dict, Optional, Sequence, cast

import google.generativeai as genai
from google.generativeai.types import generation_types
from llama_index.core.base.llms.types import (
    ChatMessage,
    ChatResponse,
    ChatResponseAsyncGen,
    ChatResponseGen,
    CompletionResponse,
    CompletionResponseGen,
    CompletionResponseAsyncGen,
    LLMMetadata,
    MessageRole,
)
from llama_index.core.bridge.pydantic import Field, PrivateAttr
from llama_index.core.callbacks import CallbackManager
from llama_index.core.constants import DEFAULT_NUM_OUTPUTS, DEFAULT_TEMPERATURE
from llama_index.core.llms.llm import ToolSelection
from llama_index.core.llms.callbacks import llm_chat_callback, llm_completion_callback
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.utilities.gemini_utils import (
    merge_neighboring_same_role_messages,
)

from .utils import (
    chat_from_gemini_response,
    chat_message_to_gemini,
    completion_from_gemini_response,
)

GEMINI_MODELS = (
    "models/gemini-2.0-flash-exp",
    "models/gemini-2.0-flash-001",
    # Gemini 1.0 Pro Vision has been deprecated on July 12, 2024.
    # According to official recommendations, switch the default model to gemini-1.5-flash
    "models/gemini-1.5-flash",
    "models/gemini-1.5-flash-latest",
    "models/gemini-pro",
    "models/gemini-pro-latest",
    "models/gemini-1.5-pro",
    "models/gemini-1.5-pro-latest",
    "models/gemini-1.0-pro",
    # for some reason, google lists this without the models prefix
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.0-pro",
)

if TYPE_CHECKING:
    from llama_index.core.tools.types import BaseTool


class Gemini(FunctionCallingLLM):
    """
    Gemini LLM.

    Examples:
        `pip install llama-index-llms-gemini`

        ```python
        from llama_index.llms.gemini import Gemini

        llm = Gemini(model="models/gemini-ultra", api_key="YOUR_API_KEY")
        resp = llm.complete("Write a poem about a magic backpack")
        print(resp)
        ```
    """

    model: str = Field(default=GEMINI_MODELS[0], description="The Gemini model to use.")
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE,
        description="The temperature to use during generation.",
        ge=0.0,
        le=1.0,
    )
    max_tokens: int = Field(
        default=DEFAULT_NUM_OUTPUTS,
        description="The number of tokens to generate.",
        gt=0,
    )
    generate_kwargs: dict = Field(
        default_factory=dict, description="Kwargs for generation."
    )

    _model: genai.GenerativeModel = PrivateAttr()
    _model_meta: genai.types.Model = PrivateAttr()
    _request_options: Optional[genai.types.RequestOptions] = PrivateAttr()

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = GEMINI_MODELS[0],
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: Optional[int] = None,
        generation_config: Optional[genai.types.GenerationConfigDict] = None,
        safety_settings: Optional[genai.types.SafetySettingDict] = None,
        callback_manager: Optional[CallbackManager] = None,
        api_base: Optional[str] = None,
        transport: Optional[str] = None,
        model_name: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
        request_options: Optional[genai.types.RequestOptions] = None,
        **generate_kwargs: Any,
    ):
        """Creates a new Gemini model interface."""
        if model_name is not None:
            warnings.warn(
                "model_name is deprecated, please use model instead",
                DeprecationWarning,
            )

            model = model_name

        # API keys are optional. The API can be authorised via OAuth (detected
        # environmentally) or by the GOOGLE_API_KEY environment variable.
        config_params: Dict[str, Any] = {
            "api_key": api_key or os.getenv("GOOGLE_API_KEY"),
        }
        if api_base:
            config_params["client_options"] = {"api_endpoint": api_base}
        if transport:
            config_params["transport"] = transport
        if default_headers:
            default_metadata = []
            for key, value in default_headers.items():
                default_metadata.append((key, value))
            # `default_metadata` contains (key, value) pairs that will be sent with every request.
            # When using `transport="rest"`, these will be sent as HTTP headers.
            config_params["default_metadata"] = default_metadata
        # transport: A string, one of: [`rest`, `grpc`, `grpc_asyncio`].
        genai.configure(**config_params)

        base_gen_config = generation_config if generation_config else {}
        # Explicitly passed args take precedence over the generation_config.
        final_gen_config = cast(
            generation_types.GenerationConfigDict,
            {"temperature": temperature, **base_gen_config},
        )

        model_meta = genai.get_model(model)

        genai_model = genai.GenerativeModel(
            model_name=model,
            generation_config=final_gen_config,
            safety_settings=safety_settings,
        )

        supported_methods = model_meta.supported_generation_methods
        if "generateContent" not in supported_methods:
            raise ValueError(
                f"Model {model} does not support content generation, only "
                f"{supported_methods}."
            )

        if not max_tokens:
            max_tokens = model_meta.output_token_limit
        else:
            max_tokens = min(max_tokens, model_meta.output_token_limit)

        super().__init__(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            generate_kwargs=generate_kwargs,
            callback_manager=callback_manager,
        )

        self._model_meta = model_meta
        self._model = genai_model
        self._request_options = request_options

    @classmethod
    def class_name(cls) -> str:
        return "Gemini_LLM"

    @property
    def metadata(self) -> LLMMetadata:
        total_tokens = self._model_meta.input_token_limit + self.max_tokens
        return LLMMetadata(
            context_window=total_tokens,
            num_output=self.max_tokens,
            model_name=self.model,
            is_chat_model=True,
            # All gemini models support function calling
            is_function_calling_model=True,
        )

    @llm_completion_callback()
    def complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        request_options = self._request_options or kwargs.pop("request_options", None)
        result = self._model.generate_content(
            prompt, request_options=request_options, **kwargs
        )
        return completion_from_gemini_response(result)

    @llm_completion_callback()
    async def acomplete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        request_options = self._request_options or kwargs.pop("request_options", None)
        result = await self._model.generate_content_async(
            prompt, request_options=request_options, **kwargs
        )
        return completion_from_gemini_response(result)

    @llm_completion_callback()
    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        request_options = self._request_options or kwargs.pop("request_options", None)

        def gen():
            it = self._model.generate_content(
                prompt, stream=True, request_options=request_options, **kwargs
            )
            for r in it:
                yield completion_from_gemini_response(r)

        return gen()

    @llm_completion_callback()
    def astream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseAsyncGen:
        request_options = self._request_options or kwargs.pop("request_options", None)

        async def gen():
            it = await self._model.generate_content_async(
                prompt, stream=True, request_options=request_options, **kwargs
            )
            async for r in it:
                yield completion_from_gemini_response(r)

        return gen()

    @llm_chat_callback()
    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        request_options = self._request_options or kwargs.pop("request_options", None)
        merged_messages = merge_neighboring_same_role_messages(messages)
        *history, next_msg = map(chat_message_to_gemini, merged_messages)
        chat = self._model.start_chat(history=history)
        response = chat.send_message(
            next_msg,
            request_options=request_options,
            **kwargs,
        )
        return chat_from_gemini_response(response)

    @llm_chat_callback()
    async def achat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponse:
        request_options = self._request_options or kwargs.pop("request_options", None)
        merged_messages = merge_neighboring_same_role_messages(messages)
        *history, next_msg = map(chat_message_to_gemini, merged_messages)
        chat = self._model.start_chat(history=history)
        response = await chat.send_message_async(
            next_msg, request_options=request_options, **kwargs
        )
        return chat_from_gemini_response(response)

    @llm_chat_callback()
    def stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseGen:
        request_options = self._request_options or kwargs.pop("request_options", None)
        merged_messages = merge_neighboring_same_role_messages(messages)
        *history, next_msg = map(chat_message_to_gemini, merged_messages)
        chat = self._model.start_chat(history=history)
        response = chat.send_message(
            next_msg, stream=True, request_options=request_options, **kwargs
        )

        def gen() -> ChatResponseGen:
            content = ""
            for r in response:
                top_candidate = r.candidates[0]
                content_delta = top_candidate.content.parts[0].text
                content += content_delta
                llama_resp = chat_from_gemini_response(r)
                llama_resp.delta = content_delta
                llama_resp.message.content = content
                yield llama_resp

        return gen()

    @llm_chat_callback()
    async def astream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseAsyncGen:
        request_options = self._request_options or kwargs.pop("request_options", None)
        merged_messages = merge_neighboring_same_role_messages(messages)
        *history, next_msg = map(chat_message_to_gemini, merged_messages)
        chat = self._model.start_chat(history=history)
        response = await chat.send_message_async(
            next_msg, stream=True, request_options=request_options, **kwargs
        )

        async def gen() -> ChatResponseAsyncGen:
            content = ""
            async for r in response:
                top_candidate = r.candidates[0]
                content_delta = top_candidate.content.parts[0].text
                content += content_delta
                llama_resp = chat_from_gemini_response(r)
                llama_resp.delta = content_delta
                llama_resp.message.content = content
                yield llama_resp

        return gen()

    def _prepare_chat_with_tools(
        self,
        tools: Sequence["BaseTool"],
        user_msg: Optional[Union[str, ChatMessage]] = None,
        chat_history: Optional[List[ChatMessage]] = None,
        verbose: bool = False,
        allow_parallel_tool_calls: bool = False,
        tool_choice: Union[str, dict] = "auto",
        strict: Optional[bool] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Predict and call the tool."""
        from google.generativeai.types import FunctionDeclaration, ToolDict

        tool_declarations = []
        for tool in tools:
            descriptions = {}
            for param_name, param_schema in tool.metadata.get_parameters_dict()[
                "properties"
            ].items():
                param_description = param_schema.get("description", None)
                if param_description:
                    descriptions[param_name] = param_description

            tool.metadata.fn_schema.__doc__ = tool.metadata.description
            tool_declarations.append(
                FunctionDeclaration.from_function(tool.metadata.fn_schema, descriptions)
            )

        if isinstance(user_msg, str):
            user_msg = ChatMessage(role=MessageRole.USER, content=user_msg)

        messages = chat_history or []
        if user_msg:
            messages.append(user_msg)

        return {
            "messages": messages,
            "tools": ToolDict(function_declarations=tool_declarations)
            if tool_declarations
            else None,
            **kwargs,
        }

    def get_tool_calls_from_response(
        self,
        response: ChatResponse,
        error_on_no_tool_call: bool = True,
        **kwargs: Any,
    ) -> List[ToolSelection]:
        """Predict and call the tool."""
        tool_calls = response.message.additional_kwargs.get("tool_calls", [])

        if len(tool_calls) < 1:
            if error_on_no_tool_call:
                raise ValueError(
                    f"Expected at least one tool call, but got {len(tool_calls)} tool calls."
                )
            else:
                return []

        tool_selections = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, genai.protos.FunctionCall):
                raise ValueError("Invalid tool_call object")

            tool_selections.append(
                ToolSelection(
                    tool_id=str(uuid.uuid4()),
                    tool_name=tool_call.name,
                    tool_kwargs=dict(tool_call.args),
                )
            )

        return tool_selections
