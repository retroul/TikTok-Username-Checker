import asyncio
import aiohttp
import os

def write_file(username: str) -> None:
    with open("hits.txt", "a", encoding="UTF-8") as f:
        f.write(f"{username}\n")

class Checker:
    def __init__(self, usernames):
        self.to_check = usernames
        self.sem = asyncio.Semaphore(10)

    async def _check(self, session, username):
        url = f"https://www.tiktok.com/@{username}"
        try:
            async with self.sem, session.head(url, timeout=10) as response:
                if response.status == 200:
                    print(f"\033[31;1m[UNAVAILABLE]\033[0m {url}")
                else:
                    print(f"\033[32;1m[AVAILABLE]\033[0m {url}")
                    write_file(username)
        except Exception as e:
            print(f"\033[33;1m[ERROR]\033[0m Failed to check {username}: {e}")

    async def start(self):
        print("Checking usernames, please wait...")
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
            await asyncio.gather(*(self._check(session, u) for u in self.to_check))

if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")
    os.system("title TikTok Username Checker" if os.name == "nt" else "")

    try:
        with open("usernames.txt", encoding="UTF-8") as f:
            username_list = list(set(line.strip() for line in f if line.strip()))
    except FileNotFoundError:
        print("Error: usernames.txt not found!")
        exit(1)

    checker = Checker(username_list)
    asyncio.run(checker.start())
    