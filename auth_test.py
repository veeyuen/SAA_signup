import streamlit as st

st.set_page_config(page_title="Auth Test", layout="centered")

st.title("Auth Test")

if not st.user.is_logged_in:
    st.info("Not logged in yet.")
    st.button("Log in with Google", on_click=lambda: st.login("google"))
    st.stop()

st.success("Logged in successfully.")
st.write("Email:", st.user.get("email", ""))
st.write("Name:", st.user.get("name", ""))

st.button("Log out", on_click=st.logout)
