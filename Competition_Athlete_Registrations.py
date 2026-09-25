"""Streamlit main-entry alias for the SAA competition registration application.

The implementation remains in signup_app_public_entry.py so there is only one
copy of the registration workflow to maintain. Configure Streamlit Cloud's main
file path to this file after the deployment has been verified.
"""

from signup_app_public_entry import *  # noqa: F401,F403
