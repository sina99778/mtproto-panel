# راهنمای Cloudflare Spectrum برای پنل MTProto

Spectrum تنها راهی است که می‌توان یک پروکسی **TCP** (مثل MTProto) را واقعاً پشت
IPهای کلودفلر مخفی کرد (برخلاف CDN رایگان که فقط HTTP/WS را پراکسی می‌کند).

---

## ⛔ اول این را بخوان — واقعیت پلن (مهم)

- **Spectrum برای پورت TCP دلخواه فقط روی پلن Enterprise کلودفلر است** (به‌عنوان add-on پولی).
- **Pro و Business** فقط اپلیکیشن‌های از پیش‌تعریف‌شده (SSH / RDP / Minecraft) را پشتیبانی می‌کنند — **برای MTProto کار نمی‌کنند**.
- Enterprise قیمت‌گذاری سفارشی و معمولاً بسیار گران دارد، و Spectrum **بر اساس مصرف (per-GB)** هم شارژ می‌شود.

> **نتیجه:** اگر Enterprise نداری، Spectrum را فراموش کن. به‌جایش از همین پنل استفاده کن:
> **تونل (Paqet) + چند Endpoint + فِیل‌اوور DNS** (صفحهٔ «ضدفیلتر») — رایگان است و همان
> هدف (مخفی‌کردن IP اصلی + مقاومت در برابر فیلتر) را برآورده می‌کند.

اگر Enterprise + Spectrum داری، ادامه بده. 👇

---

## پیش‌نیازها
1. یک **دامنه روی کلودفلر** (وضعیت Active).
2. پلن **Enterprise** با **Spectrum** فعال.
3. یک پروکسی MTProto حالت **مستقیم (Direct/FakeTLS)** روی سرور خارج، روی یک پورت TCP (مثلاً `8443`).

---

## قدم ۱ — ساخت API Token
داشبورد کلودفلر → آیکن پروفایل → **My Profile → API Tokens → Create Token → Custom token**.
این Permissionها را بده:
- **Zone → Spectrum → Edit** (برای ساخت/حذف اپ Spectrum)
- **Zone → DNS → Edit** (اگر می‌خواهی فِیل‌اوور DNS هم کار کند)
- **Zone Resources:** Include → Specific zone → دامنه‌ات

توکن ساخته‌شده را کپی کن (فقط همان لحظه نمایش داده می‌شود).

## قدم ۲ — پیداکردن Zone ID
داشبورد کلودفلر → دامنه‌ات را باز کن → صفحهٔ **Overview** → ستون راست، پایین: **Zone ID** را کپی کن.

## قدم ۳ — وارد کردن در پنل
پنل → **تنظیمات** → بخش Cloudflare:
- `API Token` و `Zone ID` را بگذار و ذخیره کن.
- مطمئن شو **«IP عمومی سرور»** (همان origin) هم درست تنظیم شده است.

## قدم ۴ — ساخت پروکسی کلودفلر
پنل → **پروکسی جدید** → نوع **کلودفلر**:
- **پورت:** پورت origin که پروکسی رویش بالاست (مثلاً `8443`).
- **دامنهٔ edge کلودفلر:** زیردامنه‌ای که کاربر به آن وصل می‌شود (مثلاً `tg.example.com`).
- **پورت edge:** معمولاً `443`.
- بساز. پنل خودکار اپ Spectrum را می‌سازد.

> اگر بعداً خواستی دوباره بسازی/حذف کنی: پنل → **ضدفیلتر / Endpointها** → بخش Spectrum همان پروکسی → «ساخت/بازسازی» یا «حذف اپ».

## قدم ۵ — تأیید
- در صفحهٔ «ضدفیلتر» وضعیت باید **«فعال (app: …)»** باشد.
- لینکی که به کاربر می‌دهی به‌شکل `tg.example.com:443` با همان secret می‌شود (پنل خودکار همین را در داشبورد نشان می‌دهد).
- از موبایل تست کن.

---

## 🔒 سخت‌سازی (توصیه‌شده) — مخفی‌کردن کاملِ IP origin
بدون این مرحله، پورت origin روی سرور خارج همچنان از اینترنت قابل‌پروب است و IP اصلی لو می‌رود.
فقط به **رنج‌های IP کلودفلر** اجازهٔ دسترسی به پورت origin بده:

```bash
# اول قانون «allow عمومی» که نصب‌کننده گذاشته را بردار:
sudo ufw delete allow 8443/tcp 2>/dev/null

# فقط رنج‌های کلودفلر مجاز شوند:
for c in $(curl -s https://www.cloudflare.com/ips-v4); do sudo ufw allow from $c to any port 8443 proto tcp; done
for c in $(curl -s https://www.cloudflare.com/ips-v6); do sudo ufw allow from $c to any port 8443 proto tcp; done

# بقیه ممنوع:
sudo ufw deny 8443/tcp
```
حالا origin فقط از طریق Spectrum (کلودفلر) در دسترس است.

---

## عیب‌یابی
| نشانه | علت محتمل |
|------|-----------|
| «ساخت Spectrum ناموفق» | پلن Enterprise/Spectrum نداری، یا Token مجوز **Spectrum: Edit** ندارد، یا **Zone ID** اشتباه است |
| لینک وصل نمی‌شود | origin روی پورت درست بالا نیست، یا فایروال جلوی IPهای کلودفلر را گرفته، یا دامنهٔ edge اشتباه است |
| کند/قطع در ایران | گاهی خودِ IPهای کلودفلر هم throttle می‌شوند؛ پلن B = تونل + چرخش Endpoint |

## جمع‌بندی
- **Enterprise داری؟** → مراحل بالا، حتماً «سخت‌سازی» را هم انجام بده.
- **نداری؟** → از **تونل + Endpoint rotation + DNS failover** پنل استفاده کن (رایگان، همان نتیجه).

منابع: [Cloudflare Spectrum docs](https://developers.cloudflare.com/spectrum/) ·
[Protocols per plan](https://developers.cloudflare.com/spectrum/protocols-per-plan/) ·
[Cloudflare IP ranges](https://www.cloudflare.com/ips/)
