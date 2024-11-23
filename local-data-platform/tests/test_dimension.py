from local_data_platform.store.dimension import Dimension
from local_data_platform.product.vocab import Vocab

def test_get():
    data = [{
        'name': 'baked ham glazed with pineapple and chipotle peppers',
        'minutes': 85,
        'submitted': '2005-11-28',
        'tags': "['ham', 'time-to-make', 'course', 'main-ingredient', 'cuisine', 'preparation', 'occasion', 'north-american', 'lunch', 'main-dish', 'pork', 'american', 'mexican', 'southwestern-united-states', 'tex-mex', 'oven', 'holiday-event', 'easter', 'stove-top', 'spicy', 'christmas', 'meat', 'taste-mood', 'sweet', 'equipment', 'presentation', 'served-hot', '4-hours-or-less']",
        'n_steps': 7,
        'steps': "['mix cornstarch with a little cold water to dissolve', 'place all ingredients except for ham in a blender and blend smooth , in a small saucepan over medium heat bring to a boil them simmer till thickened', 'preheat oven to 375 f', 'place ham , cut end down , in a large baking pan and score skin', 'bake ham for 15 minutes', 'brush glaze over ham and bake for another hour or until internal temperature reads 140 f', 'baste half way through baking']",
        'description': 'sweet, smokey and spicy! go ahead and leave the seeds in if you enjoy the heat.',
        'ingredients': "['smoked ham', 'brown sugar', 'crushed pineapple', 'chipotle chile in adobo', 'adobo sauce', 'nutmeg', 'fresh ginger', 'cornstarch', 'salt']",
        'n_ingredients': 9,
        'average_rating': 5.0,
        'votes': 27,
        'Score': 4.85275401,
        'calories': 712.5,
        'category': 'Non-veg'
    }]
    tbl = Dimension(name=Vocab.LISTINGS,data=data)
    assert len(tbl.get()) == 1

