"""Verify the daemon owner's password with Linux-PAM."""

import ctypes as c
import os
import pwd


class Message(c.Structure):
    _fields_ = [("style", c.c_int), ("text", c.c_char_p)]


class Response(c.Structure):
    _fields_ = [("text", c.c_void_p), ("code", c.c_int)]


Conversation = c.CFUNCTYPE(
    c.c_int, c.c_int, c.POINTER(c.POINTER(Message)), c.POINTER(c.POINTER(Response)), c.c_void_p
)


class PamConversation(c.Structure):
    _fields_ = [("callback", Conversation), ("data", c.c_void_p)]


def authenticate(username, password):
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid or username != pwd.getpwuid(uid).pw_name:
        return False
    if not password or "\0" in password:
        return False

    pam = c.CDLL("libpam.so.0")
    libc = c.CDLL(None)
    libc.calloc.argtypes = [c.c_size_t, c.c_size_t]
    libc.calloc.restype = c.c_void_p
    libc.strdup.argtypes = [c.c_char_p]
    libc.strdup.restype = c.c_void_p
    libc.free.argtypes = [c.c_void_p]

    @Conversation
    def converse(count, messages, output, _):
        # PAM takes ownership of the C allocations only on success.
        if not 1 <= count <= 32:
            return 19  # PAM_CONV_ERR
        allocation = libc.calloc(count, c.sizeof(Response))
        if not allocation:
            return 5  # PAM_BUF_ERR
        answers = c.cast(allocation, c.POINTER(Response))
        status = 0
        for index in range(count):
            style = messages[index].contents.style
            if style in (1, 2):  # PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON
                answers[index].text = libc.strdup(password_bytes if style == 1 else username_bytes)
                if not answers[index].text:
                    status = 5
                    break
            elif style not in (3, 4):  # PAM_ERROR_MSG, PAM_TEXT_INFO
                status = 19
                break
        if status:
            for index in range(count):
                libc.free(answers[index].text)
            libc.free(allocation)
            return status
        output[0] = answers
        return 0

    username_bytes, password_bytes = username.encode(), password.encode()
    handle = c.c_void_p()
    pam.pam_start.argtypes = [c.c_char_p, c.c_char_p, c.POINTER(PamConversation), c.POINTER(c.c_void_p)]
    for function in (pam.pam_authenticate, pam.pam_acct_mgmt, pam.pam_end):
        function.argtypes = [c.c_void_p, c.c_int]
    status = pam.pam_start(b"system-local-login", username_bytes, c.byref(PamConversation(converse, None)), c.byref(handle))
    if status:
        return False
    try:
        status = pam.pam_authenticate(handle, 1)  # PAM_DISALLOW_NULL_AUTHTOK
        if status == 0:
            status = pam.pam_acct_mgmt(handle, 0)
        return status == 0
    finally:
        pam.pam_end(handle, status)
