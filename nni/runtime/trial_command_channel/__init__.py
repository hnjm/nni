# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

__all__ = [
    'ParameterRecord', 'TrialCommandChannel',
    'get_default_trial_command_channel',
    'set_default_trial_command_channel',
]

from typing import Optional

from .base import ParameterRecord, TrialCommandChannel

_channel: Optional[TrialCommandChannel] = None


def set_default_trial_command_channel(channel: Optional[TrialCommandChannel] = None) -> TrialCommandChannel:
    """
    Set the default trial command channel.

    If no channel is provided, we create the channel following these rules:

    - If the environment variable ``NNI_PLATFORM`` is not set or is ``unittest``, create
      :class:`~nni.runtime.trial_command_channel.standalone.StandaloneTrialCommandChannel`;
    - Otherwise, create :class:`~nni.runtime.trial_command_channel.local_legacy.LocalLegacyTrialCommandChannel`.

    Parameters
    ----------
    channel
        The channel to set. If ``None``, a default channel will be created.

    Returns
    -------
    The channel that is set.
    """
    global _channel

    if channel is not None:
        _channel = channel

    else:
        from ..env_vars import trial_env_vars, dispatcher_env_vars

        assert dispatcher_env_vars.SDK_PROCESS != 'dispatcher'

        if trial_env_vars.NNI_PLATFORM is None or trial_env_vars.NNI_PLATFORM == 'unittest':
            from .standalone import StandaloneTrialCommandChannel
            _channel = StandaloneTrialCommandChannel()
        else:
            from .local_legacy import LocalLegacyTrialCommandChannel
            _channel = LocalLegacyTrialCommandChannel()

    return _channel


def get_default_trial_command_channel() -> TrialCommandChannel:
    """
    Get the default trial command channel.
    Create one if it does not exist.

    Returns
    -------
    The default trial command channel.
    """
    if _channel is None:
        return set_default_trial_command_channel()

    return _channel
