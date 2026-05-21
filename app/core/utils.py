import random

def get_random_avatar() -> str:
    avatars = ["newimg.svg", "newimg1.svg", "newimg2.svg", "newimg3.svg", "newimg4.svg"]
    return f"/chat-avatars/{random.choice(avatars)}"
