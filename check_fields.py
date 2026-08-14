import json
data = json.loads(open('cornell_data/FA26_CS.json').read())
course = next(c for c in data if c['catalogNbr'] == '1110')
print(list(course.keys()))
print(list(course['enrollGroups'][0].keys()))