import json, re
def norm_string(s):
    return re.sub(r'[^a-z0-9]', '', str(s).lower()) if s else ""

with open('/Users/dime/Developer/ios-zingy/App/ZingyApp/Resources/musicdata/music-index.json') as f:
    cat = json.load(f)
existing_catalog = [(norm_string(s.get('artist','')), norm_string(s.get('title','')), s.get('artist'), s.get('title')) for s in cat]

def check(artist, title):
    a_str = norm_string(artist)
    t_str = norm_string(title)
    for (cat_a, cat_t, raw_a, raw_t) in existing_catalog:
        if (t_str and cat_t) and (t_str in cat_t or cat_t in t_str):
            if (a_str and cat_a) and (a_str in cat_a or cat_a in a_str):
                return raw_a, raw_t
    return None

test_cases = [("Drake", "Toosie Slide"), ("Drake", "Toosie"), ("Kodes", "Mechant 4"), ("Kodes", "Mechant"), ("Taylor Swift", "Me"), ("Taylor Swift", "Me and you")]
for a, t in test_cases:
    print(f"Testing {a} - {t}: Match -> {check(a, t)}")
