import base64
import os
from io import BytesIO

import matplotlib.pyplot as plt
import pandas as pd
import requests
from dagster import MetadataValue, OpExecutionContext, asset
from wordcloud import STOPWORDS, WordCloud


@asset(group_name="hackernews", compute_kind="HackerNews API")
def hackernews_topstory_ids() -> pd.DataFrame:
    """
    Get up to 500 top stories from the HackerNews topstories endpoint.

    API Docs: https://github.com/HackerNews/API#new-top-and-best-stories
    """
    newstories_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
    top_500_newstories = requests.get(newstories_url).json()
    return pd.DataFrame(top_500_newstories, columns=["item_ids"])


@asset(group_name="hackernews", compute_kind="HackerNews API")
def hackernews_topstories(
    context: OpExecutionContext, hackernews_topstory_ids: pd.DataFrame
) -> pd.DataFrame:
    """
    Get items based on story ids from the HackerNews items endpoint. It may take 1-2 minutes to fetch all 500 items.

    API Docs: https://github.com/HackerNews/API#items
    """
    results = []
    for item_id in hackernews_topstory_ids["item_ids"]:
        item = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json").json()
        results.append(item)
        if len(results) % 20 == 0:
            context.log.info(f"Got {len(results)} items so far.")

    df = pd.DataFrame(results)

    # Dagster supports attaching arbitrary metadata to asset materializations. This metadata will be
    # shown in the run logs and also be displayed on the "Activity" tab of the "Asset Details" page in the UI.
    # This metadata would be useful for monitoring and maintaining the asset as you iterate.
    # Read more about in asset metadata in https://docs.dagster.io/concepts/assets/software-defined-assets#recording-materialization-metadata
    context.add_output_metadata(
        {
            "num_records": len(df),
            "preview": MetadataValue.md(df.head().to_markdown()),
        }
    )
    return df


@asset(group_name="hackernews", compute_kind="Plot", required_resource_keys={"s3"})
def hackernews_topstories_word_cloud(
    context: OpExecutionContext, hackernews_topstories: pd.DataFrame
) -> None:
    """
    Exploratory analysis: Generate a word cloud from the current top 500 HackerNews top stories.
    Embed the plot into a Markdown metadata for quick view.

    Read more about how to create word clouds in http://amueller.github.io/word_cloud/.
    """
    stopwords = set(STOPWORDS)
    stopwords.update(["Ask", "Show", "HN"])
    titles_text = " ".join([str(item) for item in hackernews_topstories["title"]])
    titles_cloud = WordCloud(stopwords=stopwords, background_color="white").generate(titles_text)

    # Generate the word cloud image
    plt.figure(figsize=(8, 8), facecolor=None)
    plt.imshow(titles_cloud, interpolation="bilinear")
    plt.axis("off")
    plt.tight_layout(pad=0)

    # Save the image to a buffer and embed the image into Markdown content for quick view
    buffer = BytesIO()
    plt.savefig(buffer, format="png")
    image_data = base64.b64encode(buffer.getvalue())
    md_content = f"![img](data:image/png;base64,{image_data.decode()})"

    # Also, upload the image to S3
    bucket_name = os.environ.get("S3_BUCKET")
    bucket_location = context.resources.s3.get_bucket_location(Bucket=bucket_name)[
        "LocationConstraint"
    ]
    file_name = "hackernews_topstories_word_cloud.png"
    context.resources.s3.upload_fileobj(buffer, bucket_name, file_name)
    s3_path = f"https://s3.{bucket_location}.amazonaws.com/{bucket_name}/{file_name}"
    context.add_output_metadata(
        {
            # Attach the Markdown content and s3 file path as metadata to the asset
            # Read about more metadata types in https://docs.dagster.io/_apidocs/ops#metadata-types
            "plot": MetadataValue.md(md_content),
            "plot_s3_path": MetadataValue.url(s3_path),
        }
    )
